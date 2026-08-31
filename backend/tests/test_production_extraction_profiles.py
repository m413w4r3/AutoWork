from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_workflow
from cti_app.application.production_parsers import (
    ParsedSource,
    Q2ArtifactProposal,
    Q2FactProposal,
    Q2SourceOutput,
    ReferenceReport,
)
from cti_app.application.production_workflow import (
    plan_q2_extraction_profiles,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionInputSnapshot,
    ProductionInputSource,
    SourceExtraction,
    SourceExtractionStatus,
    SubjectProductionRun,
    SubjectProductionStage,
)


def _source(url: str, published_at: date | None) -> ParsedSource:
    return ParsedSource(
        local_id=url.rsplit("/", 1)[-1],
        title=url.rsplit("/", 1)[-1],
        url=url,
        canonical_url=url,
        publisher="Publisher",
        published_at=published_at,
        role=SourceRole.INDEPENDENT,
    )


def _input_source(url: str, published_at: date | None) -> ProductionInputSource:
    return ProductionInputSource(
        batch_id=uuid4(),
        candidate_id=uuid4(),
        source_candidate_id=uuid4(),
        canonical_url=url,
        role=SourceRole.PRIMARY,
        title="Core",
        publisher="Publisher",
        published_at=published_at,
        tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
    )


def _snapshot(core_sources: tuple[ProductionInputSource, ...]) -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        edition_id=uuid4(),
        editorial_group_id=uuid4(),
        editorial_group_version=1,
        subject_title="Subject",
        subject_description="Description",
        actor_or_campaign="Actor",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        research_date=date(2026, 8, 1),
        core_sources=core_sources,
        captured_at=datetime.now().astimezone(),
    )


def test_policy_keeps_all_core_and_only_three_near_supporting_full() -> None:
    core_urls = ["https://example.test/core-1", "https://example.test/core-2"]
    support_urls = [f"https://example.test/support-{index}" for index in range(1, 8)]
    report = ReferenceReport(
        sources=tuple(
            [_source(core_urls[0], date(2026, 7, 10)), _source(core_urls[1], date(2026, 7, 11))]
            + [_source(url, date(2026, 7, 12 + index)) for index, url in enumerate(support_urls)]
        ),
        events=(),
    )
    snapshot = _snapshot(
        (
            _input_source(core_urls[0], date(2026, 7, 10)),
            _input_source(core_urls[1], date(2026, 7, 11)),
        )
    )

    plans = plan_q2_extraction_profiles(report, snapshot=snapshot)

    assert [plan.profile for plan in plans[:2]] == [
        ExtractionProfile.FULL,
        ExtractionProfile.FULL,
    ]
    assert sum(plan.profile is ExtractionProfile.FULL for plan in plans[2:]) == 3
    assert sum(plan.profile is ExtractionProfile.IOC_RULES for plan in plans[2:]) == 4


def test_large_corpus_never_falls_back_to_all_full() -> None:
    core_url = "https://example.test/core"
    support_urls = [f"https://example.test/support-{index}" for index in range(100)]
    report = ReferenceReport(
        sources=tuple(
            [_source(core_url, date(2026, 7, 10))]
            + [_source(url, date(2026, 7, 11)) for url in support_urls]
        ),
        events=(),
    )

    plans = plan_q2_extraction_profiles(
        report,
        snapshot=_snapshot((_input_source(core_url, date(2026, 7, 10)),)),
    )

    assert sum(plan.profile is ExtractionProfile.FULL for plan in plans) == 4
    assert sum(plan.profile is ExtractionProfile.IOC_RULES for plan in plans) == 97


def test_missing_snapshot_fails_q2_planning_instead_of_selecting_all_full() -> None:
    report = ReferenceReport(
        sources=(_source("https://example.test/source", date(2026, 7, 10)),),
        events=(),
    )

    with pytest.raises(ValueError, match="q2_extraction_plan_missing_snapshot"):
        plan_q2_extraction_profiles(report)


def test_policy_sends_old_and_undated_supporting_to_ioc_rules() -> None:
    report = ReferenceReport(
        sources=(
            _source("https://example.test/core", date(2026, 7, 10)),
            _source("https://example.test/old", date(2025, 1, 1)),
            _source("https://example.test/undated", None),
        ),
        events=(),
    )
    plans = plan_q2_extraction_profiles(
        report,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert plans[1].profile is ExtractionProfile.IOC_RULES
    assert plans[2].profile is ExtractionProfile.IOC_RULES
    assert plans[2].reason == "historical_supporting"


def test_policy_tie_break_is_published_descending_then_url() -> None:
    urls = [
        "https://example.test/z",
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    ]
    report = ReferenceReport(
        sources=tuple(
            [_source("https://example.test/core", date(2026, 7, 10))]
            + [_source(url, date(2026, 7, 9)) for url in urls]
        ),
        events=(),
    )
    plans = plan_q2_extraction_profiles(
        report,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert [plan.canonical_url for plan in plans if plan.profile is ExtractionProfile.FULL] == [
        "https://example.test/core",
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    ]


def test_full_output_projection_for_ioc_rules_drops_narrative_facts() -> None:
    output = Q2SourceOutput(
        facts=[
            Q2FactProposal(
                category="malware",
                value="ExampleRAT",
                context="narrative",
                evidence_quote="ExampleRAT appears",
            ),
            Q2FactProposal(
                category="files",
                value="dropper.exe",
                context="IOC file",
                evidence_quote="dropper.exe is listed",
            ),
        ],
        artifacts=[
            Q2ArtifactProposal(
                artifact_type="domain",
                value="c2.example.org",
                indicator_status="confirmed_ioc",
                context="C2",
                evidence_quote="c2.example.org",
            )
        ],
    )

    projected = production_workflow.project_q2_source_output(output, ExtractionProfile.IOC_RULES)

    assert [fact.category for fact in projected.facts] == ["files"]
    assert len(projected.artifacts) == 1


class _CacheRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, ...], SourceExtraction] = {}

    @staticmethod
    def _key(values: dict[str, str]) -> tuple[str, ...]:
        return tuple(
            values[key]
            for key in (
                "source_content_sha256",
                "profile",
                "contract_version",
                "prompt_version",
                "parser_version",
                "verifier_version",
            )
        )

    async def get_by_identity(self, **values: str) -> SourceExtraction | None:
        return self.rows.get(self._key(values))

    async def find_any(self, source_content_sha256: str) -> list[SourceExtraction]:
        return [
            row for row in self.rows.values() if row.source_content_sha256 == source_content_sha256
        ]

    async def claim(self, extraction: SourceExtraction, *, force: bool = False) -> bool:
        identity = {
            "source_content_sha256": extraction.source_content_sha256,
            "profile": extraction.profile.value,
            "contract_version": extraction.contract_version,
            "prompt_version": extraction.prompt_version,
            "parser_version": extraction.parser_version,
            "verifier_version": extraction.verifier_version,
        }
        key = self._key(identity)
        existing = self.rows.get(key)
        if (
            existing is not None
            and existing.status
            in {
                SourceExtractionStatus.RUNNING,
                SourceExtractionStatus.VERIFIED,
            }
            and not force
        ):
            return False
        if existing is not None:
            extraction = replace(extraction, id=existing.id, created_at=existing.created_at)
        self.rows[key] = extraction
        return True

    async def save(self, extraction: SourceExtraction) -> None:
        identity = {
            "source_content_sha256": extraction.source_content_sha256,
            "profile": extraction.profile.value,
            "contract_version": extraction.contract_version,
            "prompt_version": extraction.prompt_version,
            "parser_version": extraction.parser_version,
            "verifier_version": extraction.verifier_version,
        }
        self.rows[self._key(identity)] = extraction


class _CacheStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, object]] = {}

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        return self.payloads[blob_id]

    async def store_source_extraction_payloads(
        self, *, raw: str, canonical: dict[str, object]
    ) -> tuple[UUID | None, UUID]:
        raw_id, canonical_id = uuid4(), uuid4()
        self.payloads[raw_id] = {"raw": raw}
        self.payloads[canonical_id] = canonical
        return raw_id, canonical_id


class _CacheUow:
    def __init__(self, state: _CacheState) -> None:
        self._state = state
        self.source_extractions = state.extractions
        self.source_collections = state.collections
        self.source_documents = state.documents
        self.production_artifacts = state.artifacts
        self.subject_production_runs = state.runs

    async def __aenter__(self) -> _CacheUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def commit(self) -> None:
        return None


class _CacheState:
    def __init__(
        self,
        docs: dict[UUID, SimpleNamespace],
        collections: dict[UUID, SimpleNamespace],
    ) -> None:
        self.extractions = _CacheRepository()
        self.documents = SimpleNamespace(
            list_for_subject=lambda subject_id: self._documents(subject_id, docs)
        )
        self.collections = SimpleNamespace(
            list_for_subject=lambda subject_id: self._collections(subject_id, collections)
        )
        self.artifacts = SimpleNamespace(
            get_current=lambda run_id, stage: self._reference_artifact(run_id, stage)
        )
        self.runs = SimpleNamespace(get=lambda run_id: self._run(run_id))
        self._docs_by_id = docs
        self._collections_by_id = collections
        self._runs: dict[UUID, SubjectProductionRun] = {}

    async def _documents(
        self, subject_id: UUID, docs: dict[UUID, SimpleNamespace]
    ) -> list[SimpleNamespace]:
        return [doc for doc in docs.values() if doc.subject_id == subject_id]

    async def _collections(
        self, subject_id: UUID, collections: dict[UUID, SimpleNamespace]
    ) -> list[SimpleNamespace]:
        return [item for item in collections.values() if item.subject_id == subject_id]

    async def _reference_artifact(self, run_id: UUID, stage: str) -> SimpleNamespace:
        del run_id, stage
        return SimpleNamespace(canonical_blob_id=uuid4(), input_hash="a" * 64)

    async def _run(self, run_id: UUID) -> SubjectProductionRun | None:
        return self._runs.get(run_id)


class _ArchivedBlobs:
    """Blob catalog holding the archived captures Q2 must analyse."""

    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}

    def add(self, content: bytes) -> tuple[UUID, str]:
        blob_id = uuid4()
        self.contents[blob_id] = content
        return blob_id, hashlib.sha256(content).hexdigest()

    async def read_blob(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        del max_bytes
        content = self.contents.get(blob_id)
        if content is None:
            raise KeyError(blob_id)
        return content


def _archived_document(
    blobs: _ArchivedBlobs,
    *,
    subject_id: UUID,
    url: str,
    content: bytes,
) -> SimpleNamespace:
    blob_id, sha256 = blobs.add(content)
    return SimpleNamespace(
        id=uuid4(),
        subject_id=subject_id,
        final_url=url,
        decoded_sha256=sha256,
        decoded_blob_id=blob_id,
        detected_mime_type="text/plain",
    )


def _collection_for(document: SimpleNamespace, url: str) -> SimpleNamespace:
    return SimpleNamespace(
        subject_id=document.subject_id,
        canonical_url=url,
        source_document_id=document.id,
        decoded_blob_id=document.decoded_blob_id,
    )


class _CacheGateway:
    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.source_ids: list[str] = []
        self.prompts: list[str] = []

    async def execute(self, request: object, role: object) -> object:
        del role
        run_id = request.run_id
        self.calls.append(run_id)
        self.prompts.append(request.text)
        source_id = (
            request.metadata.get("source_id") or request.metadata["source_url"].rsplit("/", 1)[-1]
        )
        self.source_ids.append(str(source_id))
        return SimpleNamespace(
            output_text=(
                "# FACTS\n\n## malware\n- ExampleRAT :: observed\n"
                "# IOCS\n\n## confirmed\ndomain:\n- c2.example.org\n"
            ),
            run=SimpleNamespace(
                id=run_id,
                status=production_workflow.ModelRunStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                error_details=None,
            ),
            metadata={},
        )


class _ExtractionSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def store_extraction_result(self, **values: object) -> SimpleNamespace:
        self.calls.append(values)
        return SimpleNamespace(id=uuid4())


class _Diagnostics:
    def record(self, **values: object) -> None:
        del values

    def record_parse(self, **values: object) -> None:
        del values


def _cached_orchestrator(
    state: _CacheState,
    gateway: _CacheGateway,
    store: _CacheStore,
    sink: _ExtractionSink,
    monkeypatch: pytest.MonkeyPatch,
    blobs: _ArchivedBlobs | None = None,
) -> production_workflow.ProductionWorkflowOrchestrator:
    orchestrator = production_workflow.ProductionWorkflowOrchestrator.__new__(
        production_workflow.ProductionWorkflowOrchestrator
    )
    orchestrator._blob_reader = blobs
    orchestrator._uow_factory = lambda: _CacheUow(state)
    orchestrator._model_gateway = gateway
    orchestrator._artifact_store = store
    orchestrator._extraction = sink
    orchestrator._diagnostics = _Diagnostics()
    orchestrator._correlation_id = "test"
    orchestrator._pacing = SimpleNamespace(model_delay_seconds=lambda: 0.0)

    async def no_reuse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    async def subject_context(*args: object) -> tuple[str, str]:
        del args
        return "Subject", ""

    async def production_context(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return SimpleNamespace(
            external_llm_allowed=True,
            subject_title="Subject",
            period_start="2026-07-01",
            period_end="2026-07-31",
        )

    orchestrator._reuse_artifact = no_reuse
    orchestrator._subject_context = subject_context
    monkeypatch.setattr(
        production_workflow,
        "build_subject_production_context",
        production_context,
    )
    return orchestrator


@pytest.mark.asyncio
async def test_source_cache_reuses_between_subjects_and_projects_full_for_light(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_subject, second_subject = uuid4(), uuid4()
    url = "https://example.test/shared"
    blobs = _ArchivedBlobs()
    content = b"ARCHIVED VERSION"
    first_document = _archived_document(blobs, subject_id=first_subject, url=url, content=content)
    # Two subjects, one identical archived capture: the same content hash.
    second_document = _archived_document(blobs, subject_id=second_subject, url=url, content=content)
    docs = {first_document.id: first_document, second_document.id: second_document}
    collections = {
        uuid4(): _collection_for(first_document, url),
        uuid4(): _collection_for(second_document, url),
    }
    state = _CacheState(docs, collections)
    gateway, store, sink = _CacheGateway(), _CacheStore(), _ExtractionSink()
    orchestrator = _cached_orchestrator(state, gateway, store, sink, monkeypatch, blobs)
    report = ReferenceReport(sources=(_source(url, date(2026, 7, 10)),), events=())

    first_run = SubjectProductionRun(
        subject_id=first_subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    second_run = SubjectProductionRun(
        subject_id=second_subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[first_run.id] = first_run
    state._runs[second_run.id] = second_run

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return report

    orchestrator._load_reference_report = load_report
    snapshot = _snapshot((_input_source(url, date(2026, 7, 10)),))
    first = await orchestrator._execute_direct_url_extraction(first_run, snapshot=snapshot)
    second = await orchestrator._execute_direct_url_extraction(second_run, snapshot=snapshot)

    assert first["status"] == "success", first
    assert second["status"] == "success"
    assert len(gateway.calls) == 1
    assert second["model_calls_avoided"] == 1
    # The archived capture is what the model was given, and the checkpoint is
    # recorded under the hash of exactly that content.
    assert "ARCHIVED VERSION" in gateway.prompts[0]
    assert "<ARCHIVED_SOURCE>" in gateway.prompts[0]
    stored = list(state.extractions.rows.values())
    assert [row.source_content_sha256 for row in stored] == [first_document.decoded_sha256]
    assert stored[0].status is SourceExtractionStatus.VERIFIED
    second_items = sink.calls[-1]["canonical_json"]["items"]  # type: ignore[index]
    assert all(item["source_ids"] == [report.sources[0].local_id] for item in second_items)
    cached_payload = next(iter(store.payloads.values()))
    assert "subject_id" not in cached_payload
    assert "source_ids" not in cached_payload

    # The same cached FULL result satisfies a later IOC_RULES plan.
    light_snapshot = _snapshot(
        (_input_source("https://example.test/other-core", date(2026, 7, 10)),)
    )
    third_run = SubjectProductionRun(
        subject_id=second_subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    old_report = ReferenceReport(sources=(_source(url, date(2025, 1, 1)),), events=())

    async def load_old_report(*args: object) -> ReferenceReport:
        del args
        return old_report

    orchestrator._load_reference_report = load_old_report
    third_result = await orchestrator._execute_direct_url_extraction(
        third_run, snapshot=light_snapshot
    )
    assert third_result["status"] == "success"
    assert len(gateway.calls) == 1
    assert all(item["category"] != "malware" for item in sink.calls[-1]["canonical_json"]["items"])


@pytest.mark.asyncio
async def test_ioc_rules_cache_does_not_satisfy_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = uuid4()
    url = "https://example.test/profile-change"
    blobs = _ArchivedBlobs()
    document = _archived_document(
        blobs, subject_id=subject, url=url, content=b"ARCHIVED PROFILE CHANGE"
    )
    collection = _collection_for(document, url)
    state = _CacheState({document.id: document}, {uuid4(): collection})
    gateway, store, sink = _CacheGateway(), _CacheStore(), _ExtractionSink()
    orchestrator = _cached_orchestrator(state, gateway, store, sink, monkeypatch, blobs)

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return ReferenceReport(sources=(_source(url, date(2025, 1, 1)),), events=())

    orchestrator._load_reference_report = load_report
    light_snapshot = _snapshot((_input_source("https://example.test/other", date(2026, 7, 10)),))
    first_run = SubjectProductionRun(
        subject_id=subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[first_run.id] = first_run
    first = await orchestrator._execute_direct_url_extraction(first_run, snapshot=light_snapshot)

    async def load_core_report(*args: object) -> ReferenceReport:
        del args
        return ReferenceReport(sources=(_source(url, date(2026, 7, 10)),), events=())

    orchestrator._load_reference_report = load_core_report
    full_snapshot = _snapshot((_input_source(url, date(2026, 7, 10)),))
    second_run = SubjectProductionRun(
        subject_id=subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[second_run.id] = second_run
    second = await orchestrator._execute_direct_url_extraction(second_run, snapshot=full_snapshot)

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_retry_uses_source_cache_for_s1_to_s10_and_calls_only_s11(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = uuid4()
    urls = [f"https://example.test/S{index}" for index in range(1, 12)]
    blobs = _ArchivedBlobs()
    docs: dict[UUID, SimpleNamespace] = {}
    collections: dict[UUID, SimpleNamespace] = {}
    for url in urls:
        document = _archived_document(
            blobs,
            subject_id=subject,
            url=url,
            content=f"ARCHIVED {url}".encode(),
        )
        docs[document.id] = document
        collections[uuid4()] = _collection_for(document, url)
    state = _CacheState(docs, collections)
    gateway, store, sink = _CacheGateway(), _CacheStore(), _ExtractionSink()
    orchestrator = _cached_orchestrator(state, gateway, store, sink, monkeypatch, blobs)
    report = ReferenceReport(
        sources=tuple(_source(url, date(2026, 7, 10)) for url in urls), events=()
    )
    run = SubjectProductionRun(
        subject_id=subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[run.id] = run

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return report

    orchestrator._load_reference_report = load_report
    snapshot = _snapshot((_input_source(urls[0], date(2026, 7, 10)),))
    first = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)
    missing_document_id = next(
        document_id for document_id, document in docs.items() if document.final_url == urls[-1]
    )
    del docs[missing_document_id]
    retry = SubjectProductionRun(
        subject_id=subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
        force_recompute_from_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[retry.id] = retry
    second = await orchestrator._execute_direct_url_extraction(retry, snapshot=snapshot)

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert gateway.source_ids[:11] == [f"S{index}" for index in range(1, 12)]
    assert gateway.source_ids[11:] == ["S11"]
    assert second["model_calls_avoided"] == 10


@pytest.mark.asyncio
async def test_changed_archived_content_hash_requires_new_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = uuid4()
    url = "https://example.test/changed"
    blobs = _ArchivedBlobs()
    document = _archived_document(blobs, subject_id=subject, url=url, content=b"ARCHIVED V1")
    collection = _collection_for(document, url)
    state = _CacheState({document.id: document}, {uuid4(): collection})
    gateway, store, sink = _CacheGateway(), _CacheStore(), _ExtractionSink()
    orchestrator = _cached_orchestrator(state, gateway, store, sink, monkeypatch, blobs)
    report = ReferenceReport(sources=(_source(url, date(2026, 7, 10)),), events=())
    run = SubjectProductionRun(
        subject_id=subject, edition_id=uuid4(), current_stage=SubjectProductionStage.EXTRACTION
    )
    state._runs[run.id] = run

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return report

    orchestrator._load_reference_report = load_report
    snapshot = _snapshot((_input_source(url, date(2026, 7, 10)),))
    first = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)
    # Same URL, re-archived with different content: a distinct extraction.
    updated_blob_id, updated_sha256 = blobs.add(b"ARCHIVED V2")
    document.decoded_blob_id = updated_blob_id
    document.decoded_sha256 = updated_sha256
    second_run = SubjectProductionRun(
        subject_id=subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[second_run.id] = second_run
    result = await orchestrator._execute_direct_url_extraction(second_run, snapshot=snapshot)

    assert result["status"] == "success", result
    assert first["status"] == "success"
    assert len(gateway.calls) == 2
    assert "ARCHIVED V1" in gateway.prompts[0]
    assert "ARCHIVED V2" in gateway.prompts[1]
    assert sorted(row.source_content_sha256 for row in state.extractions.rows.values()) == sorted(
        {hashlib.sha256(b"ARCHIVED V1").hexdigest(), updated_sha256}
    )


@pytest.mark.asyncio
async def test_unreadable_archive_never_caches_under_the_content_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hash without readable archived content stays outside the global cache."""
    subject = uuid4()
    url = "https://example.test/unreadable"
    blobs = _ArchivedBlobs()
    document = _archived_document(blobs, subject_id=subject, url=url, content=b"ARCHIVED BODY")
    # The hash is still recorded, but the archived bytes are gone.
    blobs.contents.pop(document.decoded_blob_id)
    collection = _collection_for(document, url)
    state = _CacheState({document.id: document}, {uuid4(): collection})
    gateway, store, sink = _CacheGateway(), _CacheStore(), _ExtractionSink()
    orchestrator = _cached_orchestrator(state, gateway, store, sink, monkeypatch, blobs)
    report = ReferenceReport(sources=(_source(url, date(2026, 7, 10)),), events=())
    run = SubjectProductionRun(
        subject_id=subject, edition_id=uuid4(), current_stage=SubjectProductionStage.EXTRACTION
    )
    state._runs[run.id] = run

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return report

    orchestrator._load_reference_report = load_report
    snapshot = _snapshot((_input_source(url, date(2026, 7, 10)),))
    result = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)

    assert result["status"] == "success", result
    assert len(gateway.calls) == 1
    assert state.extractions.rows == {}
    assert "<ARCHIVED_SOURCE>" not in gateway.prompts[0]
    assert url in gateway.prompts[0]

    # A second subject on the same URL cannot inherit anything either.
    other_subject = uuid4()
    other_document = _archived_document(
        blobs, subject_id=other_subject, url=url, content=b"ARCHIVED BODY"
    )
    blobs.contents.pop(other_document.decoded_blob_id)
    state._docs_by_id[other_document.id] = other_document
    state._collections_by_id[uuid4()] = _collection_for(other_document, url)
    other_run = SubjectProductionRun(
        subject_id=other_subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[other_run.id] = other_run
    second = await orchestrator._execute_direct_url_extraction(other_run, snapshot=snapshot)

    assert second["status"] == "success"
    assert len(gateway.calls) == 2
    assert state.extractions.rows == {}


@pytest.mark.asyncio
async def test_archive_too_large_for_one_request_falls_back_to_live_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No partial reading is ever stored under the hash of a whole document."""
    subject = uuid4()
    url = "https://example.test/oversized"
    blobs = _ArchivedBlobs()
    oversized = b"A" * (production_workflow.MAX_ARCHIVED_SOURCE_PROMPT_CHARS + 1)
    document = _archived_document(blobs, subject_id=subject, url=url, content=oversized)
    collection = _collection_for(document, url)
    state = _CacheState({document.id: document}, {uuid4(): collection})
    gateway, store, sink = _CacheGateway(), _CacheStore(), _ExtractionSink()
    orchestrator = _cached_orchestrator(state, gateway, store, sink, monkeypatch, blobs)
    run = SubjectProductionRun(
        subject_id=subject, edition_id=uuid4(), current_stage=SubjectProductionStage.EXTRACTION
    )
    state._runs[run.id] = run

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return ReferenceReport(sources=(_source(url, date(2026, 7, 10)),), events=())

    orchestrator._load_reference_report = load_report
    snapshot = _snapshot((_input_source(url, date(2026, 7, 10)),))
    result = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)

    assert result["status"] == "success", result
    assert len(gateway.calls) == 1
    assert "<ARCHIVED_SOURCE>" not in gateway.prompts[0]
    assert state.extractions.rows == {}


@pytest.mark.asyncio
async def test_archived_prompt_is_used_for_the_ioc_rules_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = uuid4()
    url = "https://example.test/light-archived"
    blobs = _ArchivedBlobs()
    document = _archived_document(
        blobs, subject_id=subject, url=url, content=b"ARCHIVED LIGHT VERSION"
    )
    collection = _collection_for(document, url)
    state = _CacheState({document.id: document}, {uuid4(): collection})
    gateway, store, sink = _CacheGateway(), _CacheStore(), _ExtractionSink()
    orchestrator = _cached_orchestrator(state, gateway, store, sink, monkeypatch, blobs)
    run = SubjectProductionRun(
        subject_id=subject, edition_id=uuid4(), current_stage=SubjectProductionStage.EXTRACTION
    )
    state._runs[run.id] = run

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return ReferenceReport(sources=(_source(url, date(2025, 1, 1)),), events=())

    orchestrator._load_reference_report = load_report
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/other", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert "ARCHIVED LIGHT VERSION" in gateway.prompts[0]
    stored = list(state.extractions.rows.values())
    assert [row.profile for row in stored] == [ExtractionProfile.IOC_RULES]
    assert stored[0].source_content_sha256 == document.decoded_sha256

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_workflow
from cti_app.application.model_gateway import ModelGateway, ModelRouter
from cti_app.application.production_parsers import (
    ParsedSource,
    Q2ArtifactProposal,
    Q2FactProposal,
    Q2SourceOutput,
    ReferenceReport,
    project_q2_source_output,
)
from cti_app.application.production_workflow import (
    _enforce_q2_profile,
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
from cti_app.integrations.models import FakeModelAdapter, InMemoryModelOutputStore
from tests.model_support import InMemoryModelRunUnitOfWorkFactory


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


def test_policy_assigns_full_only_to_frozen_core_sources() -> None:
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

    assert all(
        plan.profile is ExtractionProfile.FULL for plan in plans if plan.canonical_url in core_urls
    )
    assert all(
        plan.profile is ExtractionProfile.IOC_RULES
        for plan in plans
        if plan.canonical_url not in core_urls
    )
    assert sum(plan.profile is ExtractionProfile.FULL for plan in plans) == len(core_urls)


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

    assert sum(plan.profile is ExtractionProfile.FULL for plan in plans) == 1
    assert sum(plan.profile is ExtractionProfile.IOC_RULES for plan in plans) == 100


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
    assert plans[1].reason == "supporting_source"
    assert plans[2].reason == "supporting_source"


def test_policy_keeps_dated_supporting_sources_on_ioc_rules() -> None:
    urls = [
        "https://example.test/z",
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    ]
    report = ReferenceReport(
        sources=tuple(
            [_source("https://example.test/core", date(2026, 7, 10))]
            + [_source(url, date(2026, 7, 10)) for url in urls]
        ),
        events=(),
    )
    plans = plan_q2_extraction_profiles(
        report,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert plans[0].profile is ExtractionProfile.FULL
    assert all(plan.profile is ExtractionProfile.IOC_RULES for plan in plans[1:])


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

    projected = project_q2_source_output(output, ExtractionProfile.IOC_RULES)

    assert [fact.category for fact in projected.facts] == ["files"]
    assert len(projected.artifacts) == 1


def test_enforce_q2_profile_drops_all_ioc_rules_facts_and_preserves_full() -> None:
    output = Q2SourceOutput(
        facts=[
            Q2FactProposal(
                category="actors",
                value="Should not survive",
                context="narrative",
                evidence_quote="Should not survive",
            )
        ],
        artifacts=[
            Q2ArtifactProposal(
                artifact_type="domain",
                value="evil.example",
                indicator_status="confirmed_ioc",
            )
        ],
        uncertainties=["uncertain"],
    )

    enforced, warnings = _enforce_q2_profile(output, ExtractionProfile.IOC_RULES)
    assert enforced.facts == []
    assert enforced.artifacts == output.artifacts
    assert enforced.rules == output.rules
    assert enforced.uncertainties == output.uncertainties
    assert warnings == ("q2_ioc_rules_fact_dropped",)

    full, full_warnings = _enforce_q2_profile(output, ExtractionProfile.FULL)
    assert full is output
    assert full_warnings == ()


class _CacheRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, ...], SourceExtraction] = {}
        self.lookups = 0

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
        self.lookups += 1
        return self.rows.get(self._key(values))

    async def list_for_url(self, canonical_url: str) -> tuple[SourceExtraction, ...]:
        return tuple(row for row in self.rows.values() if row.canonical_url == canonical_url)

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


class _ArchivedBlobs:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}

    def add(self, content: bytes) -> tuple[UUID, str]:
        blob_id = uuid4()
        self.contents[blob_id] = content
        return blob_id, hashlib.sha256(content).hexdigest()

    async def read_blob(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        del max_bytes
        return self.contents[blob_id]


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


def _archived_document(
    *,
    subject_id: UUID,
    url: str,
    content: bytes,
    blobs: _ArchivedBlobs | None = None,
) -> SimpleNamespace:
    """One collected SourceDocument and its canonical decoded blob."""
    if blobs is None:
        blob_id = uuid4()
        content_sha256 = hashlib.sha256(content).hexdigest()
    else:
        blob_id, content_sha256 = blobs.add(content)
    return SimpleNamespace(
        id=uuid4(),
        subject_id=subject_id,
        final_url=url,
        decoded_sha256=content_sha256,
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
    def __init__(self, response: str | None = None) -> None:
        self.calls: list[UUID] = []
        self.source_ids: list[str] = []
        self.prompts: list[str] = []
        self.requests: list[object] = []
        self._response = response

    _DEFAULT_RESPONSE = (
        "FACT malware\n- ExampleRAT :: observed\nIOC confirmed domain\n- c2.example.org\n"
    )

    async def execute(self, request: object, role: object) -> object:
        del role
        run_id = request.run_id
        self.calls.append(run_id)
        self.prompts.append(request.text)
        self.requests.append(request)
        source_id = (
            request.metadata.get("source_id") or request.metadata["source_url"].rsplit("/", 1)[-1]
        )
        self.source_ids.append(str(source_id))
        return SimpleNamespace(
            output_text=self._response or self._DEFAULT_RESPONSE,
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
        self.events.append(values)

    def record_parse(self, **values: object) -> None:
        del values

    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []


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


def _individual_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    url: str,
    archived_text: bytes,
    gateway: object,
    published_at: date = date(2026, 7, 10),
) -> tuple[
    production_workflow.ProductionWorkflowOrchestrator,
    SubjectProductionRun,
    _CacheState,
    _ExtractionSink,
]:
    """One archived Q1 source analysed through the individual Q2 path."""
    subject = uuid4()
    blobs = _ArchivedBlobs()
    document = _archived_document(
        subject_id=subject,
        url=url,
        content=archived_text,
        blobs=blobs,
    )
    state = _CacheState({document.id: document}, {uuid4(): _collection_for(document, url)})
    sink = _ExtractionSink()
    orchestrator = _cached_orchestrator(
        state,
        gateway,  # type: ignore[arg-type]
        _CacheStore(),
        sink,
        monkeypatch,
        blobs,
    )

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return ReferenceReport(sources=(_source(url, published_at),), events=())

    orchestrator._load_reference_report = load_report
    run = SubjectProductionRun(
        subject_id=subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[run.id] = run
    return orchestrator, run, state, sink


def _multi_individual_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    urls: list[str],
    gateway: _CacheGateway,
) -> tuple[
    production_workflow.ProductionWorkflowOrchestrator,
    SubjectProductionRun,
    _CacheState,
]:
    subject = uuid4()
    blobs = _ArchivedBlobs()
    documents: dict[UUID, SimpleNamespace] = {}
    collections: dict[UUID, SimpleNamespace] = {}
    for index, url in enumerate(urls, start=1):
        document = _archived_document(
            subject_id=subject,
            url=url,
            content=f"ARCHIVED BODY S{index} c2.example.org".encode(),
            blobs=blobs,
        )
        documents[document.id] = document
        collections[uuid4()] = _collection_for(document, url)

    state = _CacheState(documents, collections)
    state._report_sources = [_source(url, date(2026, 7, 10)) for url in urls]
    orchestrator = _cached_orchestrator(
        state,
        gateway,
        _CacheStore(),
        _ExtractionSink(),
        monkeypatch,
        blobs,
    )

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return ReferenceReport(sources=tuple(state._report_sources), events=())

    orchestrator._load_reference_report = load_report
    run = SubjectProductionRun(
        subject_id=subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[run.id] = run
    return orchestrator, run, state


@pytest.mark.asyncio
async def test_individual_request_carries_the_exact_url_and_never_the_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.test/live"
    gateway = _CacheGateway()
    orchestrator, run, state, _ = _individual_setup(
        monkeypatch,
        url=url,
        archived_text=b"ARCHIVED BODY that Q2 must never be handed",
        gateway=gateway,
    )

    result = await orchestrator._execute_direct_url_extraction(
        run, snapshot=_snapshot((_input_source(url, date(2026, 7, 10)),))
    )

    assert result["status"] == "success", result
    prompt = gateway.prompts[0]
    assert url in prompt
    assert "ARCHIVED BODY" not in prompt
    assert "<ARCHIVED_SOURCE>" not in prompt
    assert "@@Q2IN" not in prompt
    for expected in ("tables", "code blocks", "images/screenshots"):
        assert expected in prompt
    request = gateway.requests[0]
    assert request.web_search is True
    assert request.routing_hint is production_workflow.ModelRoutingHint.WEB_RESEARCH
    assert "source_content_sha256" not in request.metadata
    # The validated live response is now a reusable source checkpoint. The
    # archive remains local evidence only; its body never entered the prompt.
    assert len(state.extractions.rows) == 1
    assert state.extractions.lookups == 1


@pytest.mark.asyncio
async def test_ioc_missing_from_the_archive_is_filtered_after_live_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archived text is not a complete representation of the publication."""
    url = "https://example.test/screenshot-report"
    gateway = _CacheGateway("IOC confirmed domain\n- visual-ioc.security-lab.io\n")
    orchestrator, run, _state, sink = _individual_setup(
        monkeypatch,
        url=url,
        archived_text=b"This report contains indicators in the screenshot below.",
        gateway=gateway,
        published_at=date(2025, 1, 1),
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/other", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    canonical = sink.calls[-1]["canonical_json"]
    assert canonical["items"] == []


@pytest.mark.asyncio
async def test_individual_ioc_rules_drops_facts_before_canonical_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.test/ioc-rules"
    gateway = _CacheGateway(
        "FACT actors\n- Should not survive\nIOC confirmed domain\n- evil.security-lab.io\n"
    )
    orchestrator, run, _state, sink = _individual_setup(
        monkeypatch,
        url=url,
        archived_text=b"ARCHIVED BODY",
        gateway=gateway,
        published_at=date(2025, 1, 1),
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/other", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    canonical = sink.calls[-1]["canonical_json"]
    assert canonical["items"] == []
    warnings = sink.calls[-1]["warnings"]
    assert isinstance(warnings, list)
    assert warnings.count("q2_ioc_rules_fact_dropped") == 1


@pytest.mark.asyncio
async def test_retry_and_new_run_reuse_the_unchanged_source_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.test/idempotent"
    adapter = FakeModelAdapter(research_text="IOC confirmed domain\n- c2.example.org\n")
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    orchestrator, run, state, _ = _individual_setup(
        monkeypatch,
        url=url,
        archived_text=b"ARCHIVED BODY",
        gateway=gateway,
    )
    snapshot = _snapshot((_input_source(url, date(2026, 7, 10)),))

    first = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)
    retry = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)

    assert first["status"] == "success", first
    assert retry["status"] == "success", retry
    assert len(adapter.calls) == 1
    assert len(model_uow.state) == 1
    assert retry["model_calls"] == 0

    # A new production run with the same archived capture reuses the same
    # source checkpoint too; a changed decoded SHA is what authorizes a read.
    next_run = SubjectProductionRun(
        subject_id=run.subject_id,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[next_run.id] = next_run
    third = await orchestrator._execute_direct_url_extraction(next_run, snapshot=snapshot)

    assert third["status"] == "success", third
    assert len(adapter.calls) == 1
    assert len(model_uow.state) == 1
    assert len(state.extractions.rows) == 1


@pytest.mark.asyncio
async def test_changed_source_hash_is_a_q2_miss_with_explainable_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.test/changed"
    gateway = _CacheGateway()
    orchestrator, run, state, _sink = _individual_setup(
        monkeypatch,
        url=url,
        archived_text=b"ARCHIVED BODY c2.example.org v1",
        gateway=gateway,
    )
    snapshot = _snapshot((_input_source(url, date(2026, 7, 10)),))
    first = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)
    assert first["status"] == "success", first

    document = next(iter(state._docs_by_id.values()))
    collection = next(iter(state._collections_by_id.values()))
    blob_reader = orchestrator._blob_reader
    assert isinstance(blob_reader, _ArchivedBlobs)
    blob_id, digest = blob_reader.add(b"ARCHIVED BODY c2.example.org v2")
    document.decoded_blob_id = blob_id
    document.decoded_sha256 = digest
    collection.decoded_blob_id = blob_id

    next_run = SubjectProductionRun(
        subject_id=run.subject_id,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[next_run.id] = next_run
    second = await orchestrator._execute_direct_url_extraction(next_run, snapshot=snapshot)

    assert second["status"] == "success", second
    assert len(gateway.calls) == 2
    reuse = [
        event
        for event in orchestrator._diagnostics.events
        if event.get("event") == "q2.source.reuse_evaluated" and event.get("run_id") == next_run.id
    ]
    assert len(reuse) == 1
    assert reuse[0]["status"] == "miss"
    assert reuse[0]["reason"] == "source_content_changed"
    started = [
        event
        for event in orchestrator._diagnostics.events
        if event.get("event") == "q2.source.started" and event.get("run_id") == next_run.id
    ]
    assert len(started) == 1


@pytest.mark.asyncio
async def test_five_source_rebuild_reuses_unchanged_q2_and_calls_once_for_s6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = [f"https://example.test/s{index}" for index in range(1, 7)]
    gateway = _CacheGateway()
    orchestrator, run, state = _multi_individual_setup(
        monkeypatch,
        urls=urls[:5],
        gateway=gateway,
    )
    snapshot = _snapshot(tuple(_input_source(url, date(2026, 7, 10)) for url in urls[:5]))

    initial = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)
    assert initial["status"] == "success", initial
    assert len(gateway.calls) == 5

    # S6 is archived after the initial extraction. Its supporting-source
    # profile still uses one individual call when it is the only uncached IOC
    # source in the rebuild.
    s6 = _archived_document(
        subject_id=run.subject_id,
        url=urls[5],
        content=b"ARCHIVED BODY S6 c2.example.org",
        blobs=orchestrator._blob_reader,
    )
    assert isinstance(orchestrator._blob_reader, _ArchivedBlobs)
    state._docs_by_id[s6.id] = s6
    state._collections_by_id[uuid4()] = _collection_for(s6, urls[5])
    state._report_sources.append(_source(urls[5], date(2026, 7, 10)))

    next_run = SubjectProductionRun(
        subject_id=run.subject_id,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[next_run.id] = next_run
    second = await orchestrator._execute_direct_url_extraction(next_run, snapshot=snapshot)

    assert second["status"] == "success", second
    assert second["cache_hits"] == 5
    assert second["model_calls"] == 1
    assert len(gateway.calls) == 6

    evaluated = [
        event
        for event in orchestrator._diagnostics.events
        if event.get("event") == "q2.source.reuse_evaluated" and event.get("run_id") == next_run.id
    ]
    # S6 is first checked against the IOC batch checkpoint and then against
    # the individual checkpoint because it is the only remaining IOC source.
    assert len(evaluated) == 7
    assert sum(event["status"] == "hit" for event in evaluated) == 5
    assert sum(event["status"] == "miss" for event in evaluated) == 2
    started = [
        event
        for event in orchestrator._diagnostics.events
        if event.get("event") == "q2.source.started" and event.get("run_id") == next_run.id
    ]
    assert len(started) == 1
    assert started[0]["source_url"] == urls[5]
    assert not [
        event
        for event in orchestrator._diagnostics.events
        if event.get("event") == "q4.started" and event.get("run_id") == next_run.id
    ]


@pytest.mark.asyncio
async def test_changed_s3_plus_new_s6_only_call_two_q2_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = [f"https://example.test/s{index}" for index in range(1, 7)]
    gateway = _CacheGateway()
    orchestrator, run, state = _multi_individual_setup(
        monkeypatch,
        urls=urls[:5],
        gateway=gateway,
    )
    snapshot = _snapshot(tuple(_input_source(url, date(2026, 7, 10)) for url in urls[:5]))
    initial = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)
    assert initial["status"] == "success", initial
    assert len(gateway.calls) == 5

    document = next(
        document for document in state._docs_by_id.values() if document.final_url == urls[2]
    )
    assert isinstance(orchestrator._blob_reader, _ArchivedBlobs)
    blob_id, digest = orchestrator._blob_reader.add(b"ARCHIVED BODY S3 c2.example.org changed")
    document.decoded_blob_id = blob_id
    document.decoded_sha256 = digest
    next_collection = next(
        collection
        for collection in state._collections_by_id.values()
        if collection.canonical_url == urls[2]
    )
    next_collection.decoded_blob_id = blob_id

    s6 = _archived_document(
        subject_id=run.subject_id,
        url=urls[5],
        content=b"ARCHIVED BODY S6 c2.example.org",
        blobs=orchestrator._blob_reader,
    )
    state._docs_by_id[s6.id] = s6
    state._collections_by_id[uuid4()] = _collection_for(s6, urls[5])
    state._report_sources.append(_source(urls[5], date(2026, 7, 10)))
    next_run = SubjectProductionRun(
        subject_id=run.subject_id,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[next_run.id] = next_run
    second = await orchestrator._execute_direct_url_extraction(next_run, snapshot=snapshot)

    assert second["status"] == "success", second
    assert second["cache_hits"] == 4
    assert second["model_calls"] == 2
    assert len(gateway.calls) == 7
    started = [
        event
        for event in orchestrator._diagnostics.events
        if event.get("event") == "q2.source.started" and event.get("run_id") == next_run.id
    ]
    assert {event["source_url"] for event in started} == {urls[2], urls[5]}

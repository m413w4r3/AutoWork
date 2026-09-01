import hashlib
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application import production_workflow
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRouter,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_parsers import (
    ParsedSource,
    ReferenceReport,
    parse_q2_proposals_markdown,
)
from cti_app.application.production_q2_batch import q2_batch_output_marker
from cti_app.application.production_workflow import _extraction_input_hash, _q2_source_model_run_id
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import (
    ModelProvider,
    ModelRole,
    ModelRunStatus,
    ModelSubmissionState,
    ModelUsage,
)
from cti_app.domain.production import (
    ProductionInputSnapshot,
    ProductionInputSource,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.integrations.models import (
    BridgeTransportError,
    FakeModelAdapter,
    InMemoryModelOutputStore,
    _bridge_http_error,
)
from tests.model_support import InMemoryModelRunRepository, InMemoryModelRunUnitOfWorkFactory


def test_q2_source_model_run_id_is_stable_per_generation_and_source() -> None:
    run_id = uuid4()
    first = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )
    assert first == _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )
    assert first != _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=1,
        source_id="S1",
        canonical_url="https://example.test/report",
    )


def test_q2_source_model_run_id_changes_when_routing_policy_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    before = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )

    monkeypatch.setattr(production_workflow, "Q2_ROUTING_POLICY_VERSION", "next")

    after = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )
    assert after != before


def test_q2_failure_classification_keeps_checkpoint_errors_out_of_coverage() -> None:
    transient = production_workflow._classify_q2_failure(
        BridgeTransportError(
            "bridge_ui_timeout",
            "transport failure",
            retryable=True,
            phase="pre_submission",
            submission_state="pre_submission",
        )
    )
    reconciliation = production_workflow._classify_q2_failure(
        ModelSubmissionReconciliationRequiredError()
    )
    content = production_workflow._classify_q2_failure(
        BridgeTransportError(
            "source_content_invalid",
            "source response is unusable",
            retryable=False,
            phase="response_validation",
            submission_state="post_submission",
        )
    )
    control = production_workflow._classify_q2_failure(
        ModelGatewayError("Failed ModelRun is not safe to resubmit")
    )

    assert transient.status == "transient_error"
    assert transient.failure_class.value == "global_transient_pre_submission"
    assert not transient.contributes_to_coverage
    assert reconciliation.error_code == "model_submission_reconciliation_required"
    assert reconciliation.failure_class.value == "reconciliation_required"
    assert content.failure_class.value == "source_content_failure"
    assert content.contributes_to_coverage
    assert control.failure_class.value == "control_invariant_failure"
    assert not control.contributes_to_coverage


def test_iana_snapshot_bump_recomputes_extraction_without_new_q2_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    before_artifact = _extraction_input_hash(
        subject_id=run_id,
        references_hash="references",
        source_urls=["https://example.test/report"],
        pipeline_generation=0,
    )
    before_q2_run = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )

    monkeypatch.setattr(production_workflow, "IANA_TLD_SNAPSHOT_VERSION", "next-snapshot")

    after_artifact = _extraction_input_hash(
        subject_id=run_id,
        references_hash="references",
        source_urls=["https://example.test/report"],
        pipeline_generation=0,
    )
    after_q2_run = _q2_source_model_run_id(
        production_run_id=run_id,
        pipeline_generation=0,
        source_id="S1",
        canonical_url="https://example.test/report",
    )

    assert after_artifact != before_artifact
    assert after_q2_run == before_q2_run


async def test_q2_model_gateway_reuses_persisted_model_run_across_worker_replay() -> None:
    """A Q2 worker replay before artifact storage must not post a second request."""
    adapter = FakeModelAdapter()
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=FakeModelAdapter(),
            openai_structured=FakeModelAdapter(),
            qwen=FakeModelAdapter(),
            fake=adapter,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    production_run_id = uuid4()

    def q2_request(generation: int) -> ModelRequest:
        return ModelRequest(
            text="Extract the source",
            prompt_template_id="production-q2-url",
            prompt_template_version="1",
            evidence_pack_hash="a" * 64,
            external_llm_allowed=False,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            provider=ModelProvider.FAKE,
            web_search=True,
            run_id=_q2_source_model_run_id(
                production_run_id=production_run_id,
                pipeline_generation=generation,
                source_id="S1",
                canonical_url="https://example.test/report",
            ),
        )

    first = await gateway.execute(q2_request(0), ModelRole.RESEARCH)
    same_generation = await gateway.execute(q2_request(0), ModelRole.RESEARCH)
    next_generation = await gateway.execute(q2_request(1), ModelRole.RESEARCH)
    replay_before_artifact = await gateway.execute(q2_request(1), ModelRole.RESEARCH)

    assert first.run.status is ModelRunStatus.SUCCEEDED
    assert same_generation.run.id == first.run.id
    assert next_generation.run.id != first.run.id
    assert replay_before_artifact.run.status is ModelRunStatus.SUCCEEDED
    assert len(adapter.calls) == 2


def test_q2_markdown_parses_compact_facts_without_changing_windows_paths() -> None:
    parsed = parse_q2_proposals_markdown(
        """FACT infection_chain
- C:\\Windows uses other_technical and count_success
"""
    )
    assert parsed.usable
    assert parsed.value is not None
    fact = parsed.value.facts[0]
    assert fact.category == "infection_chain"
    assert fact.value == "C:\\Windows uses other_technical and count_success"
    assert fact.evidence_quote == ""


def test_bridge_timeout_codes_are_preserved() -> None:
    request = httpx.Request("POST", "https://bridge.test/v1/bridge/runs")
    for code in ("bridge_idle_timeout", "bridge_total_timeout"):
        error = _bridge_http_error(
            httpx.Response(502, request=request, json={"error": {"code": code}}), 1
        )
        assert error.code == code


class _Q2Artifacts:
    async def get_current(self, run_id: object, stage: str) -> object:
        del run_id, stage
        return type(
            "ReferenceArtifact",
            (),
            {"canonical_blob_id": uuid4(), "input_hash": "a" * 64},
        )()


class _Q2Runs:
    def __init__(self) -> None:
        self.run: SubjectProductionRun | None = None

    async def get(self, run_id: object) -> SubjectProductionRun | None:
        return self.run if self.run is not None and run_id == self.run.id else None


class _Q2Snapshots:
    def __init__(self) -> None:
        self.snapshot: ProductionInputSnapshot | None = None

    async def get_by_run(self, run_id: object) -> ProductionInputSnapshot | None:
        del run_id
        return self.snapshot


class _Q2UnitOfWork:
    def __init__(
        self,
        model_run_state: dict[Any, Any] | None = None,
        report: ReferenceReport | None = None,
    ) -> None:
        self.production_artifacts = _Q2Artifacts()
        self.subject_production_runs = _Q2Runs()
        self.production_input_snapshots = _Q2Snapshots()
        self.model_runs = InMemoryModelRunRepository(
            model_run_state if model_run_state is not None else {}
        )
        self.archive_reader = _Q2ArchiveReader()
        self.source_documents = _Q2SourceDocuments(report, self.archive_reader)
        self.source_collections = _Q2SourceCollections(self.source_documents.documents)

    async def __aenter__(self) -> "_Q2UnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


class _Q2ArchiveReader:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}

    async def read_blob(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        del max_bytes
        return self.contents[blob_id]


class _Q2SourceDocuments:
    def __init__(
        self,
        report: ReferenceReport | None,
        reader: _Q2ArchiveReader,
    ) -> None:
        self.documents: list[SimpleNamespace] = []
        for source in report.sources if report is not None else ():
            content = b"ExampleRAT" if source.local_id == "S1" else b""
            blob_id = uuid4()
            reader.contents[blob_id] = content
            self.documents.append(
                SimpleNamespace(
                    id=uuid4(),
                    final_url=source.canonical_url,
                    decoded_sha256=hashlib.sha256(content).hexdigest(),
                    decoded_blob_id=blob_id,
                    detected_mime_type="text/plain",
                )
            )

    async def list_for_subject(self, subject_id: UUID) -> list[SimpleNamespace]:
        del subject_id
        return self.documents


class _Q2SourceCollections:
    def __init__(self, documents: list[SimpleNamespace]) -> None:
        self.collections = [
            SimpleNamespace(
                canonical_url=document.final_url,
                source_document_id=document.id,
                decoded_blob_id=document.decoded_blob_id,
            )
            for document in documents
        ]

    async def list_for_subject(self, subject_id: UUID) -> list[SimpleNamespace]:
        del subject_id
        return self.collections


class _Q2Gateway:
    def __init__(self, failure: Exception | None, *, output_text: str | None = None) -> None:
        self.failure = failure
        self.output_text = output_text
        self.calls: list[str] = []

    async def execute(self, request: ModelRequest, role: ModelRole) -> object:
        del role
        source_id = str(request.metadata["source_id"])
        self.calls.append(source_id)
        if self.failure is not None:
            raise self.failure
        return type(
            "Execution",
            (),
            {
                "output_text": self.output_text
                if self.output_text is not None
                else ("FACT malware\n- ExampleRAT :: outil observe\n"),
                "run": type(
                    "Run",
                    (),
                    {
                        "id": uuid4(),
                        "status": ModelRunStatus.SUCCEEDED,
                        "error_code": None,
                        "error_message": None,
                        "error_details": None,
                    },
                )(),
                "metadata": {},
            },
        )()


class _NeedsReviewQ2Adapter:
    provider = ModelProvider.OPENAI
    requested_model = "chatgpt-web-fake"
    is_external = True

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invoke(
        self, request: Any, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del role, output_schema
        self.calls.append(request)
        return AdapterResult(
            status=AdapterResultStatus.NEEDS_REVIEW,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=1, output_tokens=0, total_tokens=1),
            metadata={
                "reason": "active_signal_stalled",
                "completion_signal": "streaming",
            },
        )

    async def resume(
        self, response_id: str, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise AssertionError("not used")


class _Q2Diagnostics:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record(self, **fields: object) -> None:
        self.events.append(fields)

    def record_parse(self, **fields: object) -> None:
        del fields

    def record_stage_outcome(self, **fields: object) -> None:
        self.events.append(fields)


class _Q2Extraction:
    def __init__(self) -> None:
        self.store_calls: list[dict[str, object]] = []

    async def store_extraction_result(self, **fields: object) -> object:
        self.store_calls.append(fields)
        return type("ExtractionArtifact", (), {"id": uuid4()})()


def _q2_report(source_count: int = 5) -> ReferenceReport:
    return ReferenceReport(
        sources=tuple(
            ParsedSource(
                local_id=f"S{index}",
                title=f"Source {index}",
                url=f"https://example.test/{index}",
                canonical_url=f"https://example.test/{index}",
                publisher="Example",
                published_at=None,
                role=SourceRole.PRIMARY,
            )
            for index in range(1, source_count + 1)
        ),
        events=(),
    )


def _q2_snapshot() -> ProductionInputSnapshot:
    return ProductionInputSnapshot(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        edition_id=uuid4(),
        editorial_group_id=uuid4(),
        editorial_group_version=1,
        subject_title="Article",
        subject_description="",
        actor_or_campaign="",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        research_date=date(2026, 8, 1),
        core_sources=(
            ProductionInputSource(
                batch_id=uuid4(),
                candidate_id=uuid4(),
                source_candidate_id=uuid4(),
                canonical_url="https://example.test/1",
                role=SourceRole.PRIMARY,
                title="Source 1",
                publisher="Example",
                published_at=None,
                tlp=TLP.CLEAR,
                sensitivity="public",
                external_llm_allowed=True,
            ),
        ),
        captured_at=datetime.now().astimezone(),
    )


def _q2_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    gateway: Any,
    report: ReferenceReport,
    *,
    model_run_state: dict[Any, Any] | None = None,
) -> tuple[
    production_workflow.ProductionWorkflowOrchestrator, SubjectProductionRun, _Q2Diagnostics
]:
    uow = _Q2UnitOfWork(model_run_state, report)
    diagnostics = _Q2Diagnostics()
    orchestrator = production_workflow.ProductionWorkflowOrchestrator.__new__(
        production_workflow.ProductionWorkflowOrchestrator
    )
    orchestrator._uow_factory = lambda: uow
    orchestrator._artifact_store = None
    orchestrator._diagnostics = diagnostics
    orchestrator._correlation_id = "test"
    orchestrator._model_gateway = gateway
    orchestrator._blob_reader = uow.archive_reader
    orchestrator._pacing = type("Pacing", (), {"model_delay_seconds": lambda self: 0.0})()
    orchestrator._extraction = _Q2Extraction()

    async def load_reference(*args: object) -> ReferenceReport:
        del args
        return report

    async def no_reuse(*args: object) -> None:
        del args
        return None

    async def subject_context(*args: object) -> tuple[str, str]:
        del args
        return "Article", ""

    async def production_context(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return type("Context", (), {"external_llm_allowed": True, "subject_title": "Article"})()

    monkeypatch.setattr(orchestrator, "_load_reference_report", load_reference)
    monkeypatch.setattr(orchestrator, "_reuse_artifact", no_reuse)
    monkeypatch.setattr(orchestrator, "_subject_context", subject_context)
    monkeypatch.setattr(production_workflow, "build_subject_production_context", production_context)
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    uow.subject_production_runs.run = run
    uow.production_input_snapshots.snapshot = _q2_snapshot()
    return orchestrator, run, diagnostics


@pytest.mark.asyncio
async def test_q2_extraction_missing_snapshot_fails_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _Q2Gateway(None)
    orchestrator, run, _ = _q2_orchestrator(monkeypatch, gateway, _q2_report(100))

    result = await orchestrator._execute_direct_url_extraction(run)

    assert result["status"] == "needs_review"
    assert result["error_code"] == "q2_extraction_plan_missing_snapshot"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_q2_duplicate_reference_source_id_fails_closed_before_batching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _Q2Gateway(None)
    report = _q2_report(2)
    duplicate_report = replace(
        report,
        sources=(
            report.sources[0],
            replace(report.sources[1], local_id=report.sources[0].local_id),
        ),
    )
    orchestrator, run, _ = _q2_orchestrator(monkeypatch, gateway, duplicate_report)

    result = await orchestrator._execute_direct_url_extraction(run, snapshot=_q2_snapshot())

    assert result["status"] == "needs_review"
    assert result["error_code"] == "duplicate_reference_source_id"
    assert gateway.calls == []


@pytest.mark.parametrize("error_code", ["bridge_ui_timeout", "transport_glitch"])
async def test_q2_retryable_source_failure_stops_before_s2_and_does_not_create_artifact(
    monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """A global retryable bridge failure must not become source coverage loss."""
    gateway = _Q2Gateway(
        BridgeTransportError(
            error_code,
            "transport failure",
            retryable=True,
            phase="pre_submission",
            submission_state="pre_submission",
        )
    )
    orchestrator, run, diagnostics = _q2_orchestrator(monkeypatch, gateway, _q2_report())

    result = await orchestrator._execute_direct_url_extraction(run, snapshot=_q2_snapshot())

    assert gateway.calls == ["S1"]
    assert result["status"] == "transient_error"
    assert result["error_code"] == error_code
    assert result["error_code"] != "q2_source_coverage_failed"
    assert result["failed_source_ids"] == ["S1"]
    assert result["source_failures"]["S1"]["submission_state"] == "pre_submission"
    assert result["source_failures"]["S1"]["phase"] == "pre_submission"
    assert result["source_failures"]["S1"]["failure_class"] == "global_transient_pre_submission"
    assert orchestrator._extraction.store_calls == []
    failed_events = [
        event for event in diagnostics.events if event.get("event") == "q2.source.failed"
    ]
    assert failed_events[0]["source_id"] == "S1"
    assert failed_events[0]["model_run_id"]
    assert failed_events[0]["retryable"] is True
    assert isinstance(failed_events[0]["duration_ms"], int)
    assert failed_events[0]["duration_ms"] >= 0


class _PersistentQ2Adapter:
    provider = ModelProvider.OPENAI
    requested_model = "chatgpt-web-fake"
    is_external = True

    def __init__(
        self,
        *,
        first_error_code: str = "bridge_ui_timeout",
        first_submission_state: str | None = "pre_submission",
    ) -> None:
        self.first_error_code = first_error_code
        self.first_submission_state = first_submission_state
        self.calls: list[Any] = []

    async def invoke(
        self, request: Any, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del role, output_schema
        self.calls.append(request)
        if len(self.calls) == 1:
            raise BridgeTransportError(
                self.first_error_code,
                "fake bridge failure",
                retryable=True,
                phase=(
                    "pre_submission"
                    if self.first_submission_state == "pre_submission"
                    else "generation"
                ),
                submission_state=self.first_submission_state,
            )
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            output_text=("FACT malware\n- ExampleRAT :: outil observe\n"),
        )

    async def resume(
        self, response_id: str, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise AssertionError("not used")


def _persistent_q2_gateway(
    adapter: Any,
) -> tuple[ModelGateway, InMemoryModelRunUnitOfWorkFactory]:
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
    return gateway, model_uow


@pytest.mark.asyncio
async def test_q2_pre_submission_retry_reuses_model_run_across_job_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted pre-submit failure is retried by the same Q2 checkpoint."""
    adapter = _PersistentQ2Adapter()
    gateway, model_uow = _persistent_q2_gateway(adapter)
    orchestrator, run, _ = _q2_orchestrator(monkeypatch, gateway, _q2_report(2))
    s1_model_run_id = _q2_source_model_run_id(
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        source_id="S1",
        canonical_url="https://example.test/1",
    )

    first = await orchestrator.execute_stage(
        run.id, SubjectProductionStage.EXTRACTION, correlation_id="test"
    )

    assert first["status"] == "transient_error"
    assert first["failed_source_ids"] == ["S1"]
    assert [call.metadata["source_id"] for call in adapter.calls] == ["S1"]
    first_run = model_uow.state[s1_model_run_id]
    assert first_run.status is ModelRunStatus.FAILED
    assert first_run.submission_state is ModelSubmissionState.NOT_SUBMITTED
    assert first_run.submission_attempt == 1
    assert adapter.calls[0].request_id == f"{s1_model_run_id}:a1"

    second = await orchestrator.execute_stage(
        run.id, SubjectProductionStage.EXTRACTION, correlation_id="test"
    )

    assert second["status"] == "success"
    assert [call.metadata["source_id"] for call in adapter.calls] == ["S1", "S1", "S2"]
    assert adapter.calls[1].request_id == f"{s1_model_run_id}:a2"
    assert model_uow.state[s1_model_run_id].id == s1_model_run_id
    assert model_uow.state[s1_model_run_id].status is ModelRunStatus.SUCCEEDED
    assert model_uow.state[s1_model_run_id].submission_attempt == 2
    assert "Failed ModelRun is not safe to resubmit" not in str(second)


@pytest.mark.asyncio
async def test_manual_extraction_retry_reuses_successful_batch_members_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = "\n\n".join(
        f"{q2_batch_output_marker(f'B{index}')}\n"
        + (f"IOC confirmed domain\n- retry-{index}.example" if index < 5 else "UNAVAILABLE")
        for index in range(1, 6)
    )
    adapter = FakeModelAdapter(research_text=response)
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    output_store = InMemoryModelOutputStore()
    gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
        ),
        model_uow,
        output_store,
    )
    orchestrator, run, _ = _q2_orchestrator(
        monkeypatch,
        gateway,
        _q2_report(5),
        model_run_state=model_uow.state,
    )
    snapshot = replace(_q2_snapshot(), core_sources=(), reuse_basis_hash="", input_hash="")

    first = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)

    assert first["status"] == "needs_review"
    assert first["completed_source_ids"] == ["S1", "S2", "S3", "S4"]
    assert len(adapter.calls) == 1

    adapter._research_text = "EMPTY"
    run.status = SubjectProductionStatus.NEEDS_REVIEW
    run.retry_from_stage(SubjectProductionStage.EXTRACTION)

    second = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)

    assert second["status"] == "success", second
    assert second["cache_hits"] == 4
    assert second["model_calls"] == 1
    assert second["light_batches"] == 0
    assert len(adapter.calls) == 2
    assert adapter.calls[-1].metadata["source_id"] == "S5"


@pytest.mark.asyncio
async def test_manual_extraction_retry_reuses_successful_full_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeModelAdapter(research_text="FACT malware\n- ExampleRAT\n")
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
    orchestrator, run, _ = _q2_orchestrator(
        monkeypatch,
        gateway,
        _q2_report(1),
        model_run_state=model_uow.state,
    )
    snapshot = _q2_snapshot()

    first = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)

    assert first["status"] == "success"
    run.status = SubjectProductionStatus.NEEDS_REVIEW
    run.retry_from_stage(SubjectProductionStage.EXTRACTION)

    second = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)

    assert second["status"] == "success", second
    assert second["cache_hits"] == 1
    assert second["model_calls"] == 0
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_q2_checkpoint_is_created_only_after_local_archive_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeModelAdapter(research_text="FACT malware\n- ExampleRAT\n")
    gateway, model_uow = _persistent_q2_gateway(adapter)
    orchestrator, run, _ = _q2_orchestrator(
        monkeypatch,
        gateway,
        _q2_report(1),
        model_run_state=model_uow.state,
    )

    first = await orchestrator._execute_direct_url_extraction(run, snapshot=_q2_snapshot())

    assert first["status"] == "success", first
    model_run_id = _q2_source_model_run_id(
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        source_id="S1",
        canonical_url="https://example.test/1",
    )
    assert model_uow.state[model_run_id].parameters.get("q2_checkpoint_keys")

    orchestrator._blob_reader.contents.clear()  # type: ignore[attr-defined]
    second = await orchestrator._execute_direct_url_extraction(run, snapshot=_q2_snapshot())

    assert second["status"] == "needs_review", second
    assert second["source_failures"]["S1"]["error_code"] == ("q2_source_evidence_unavailable")
    assert model_uow.state[model_run_id].parameters.get("q2_checkpoint_keys") == []
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_q2_submission_attempted_requires_reconciliation_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _PersistentQ2Adapter(first_submission_state="submission_attempted")
    gateway, model_uow = _persistent_q2_gateway(adapter)
    orchestrator, run, _ = _q2_orchestrator(monkeypatch, gateway, _q2_report(2))

    result = await orchestrator.execute_stage(
        run.id, SubjectProductionStage.EXTRACTION, correlation_id="test"
    )

    assert result["status"] == "needs_review"
    assert result["error_code"] == "model_submission_reconciliation_required"
    assert [call.metadata["source_id"] for call in adapter.calls] == ["S1"]
    s1_model_run_id = _q2_source_model_run_id(
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        source_id="S1",
        canonical_url="https://example.test/1",
    )
    assert model_uow.state[s1_model_run_id].status is ModelRunStatus.NEEDS_REVIEW
    assert model_uow.state[s1_model_run_id].error_code == (
        "model_submission_reconciliation_required"
    )
    assert result["source_failures"]["S1"]["failure_class"] == "reconciliation_required"


@pytest.mark.asyncio
async def test_q2_needs_review_preserves_active_signal_reason_and_never_calls_s2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _NeedsReviewQ2Adapter()
    gateway, model_uow = _persistent_q2_gateway(adapter)
    orchestrator, run, _ = _q2_orchestrator(monkeypatch, gateway, _q2_report(2))

    result = await orchestrator.execute_stage(
        run.id, SubjectProductionStage.EXTRACTION, correlation_id="test"
    )

    model_run_id = _q2_source_model_run_id(
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        source_id="S1",
        canonical_url="https://example.test/1",
    )
    failure = result["source_failures"]["S1"]
    assert result["status"] == "needs_review"
    assert result["error_code"] == "active_signal_stalled"
    assert result["error"] == "ChatGPT s'est arrêté sans produire de réponse finale."
    assert result["details"]["failure_class"] == "control_invariant_failure"
    assert failure["error_code"] == "active_signal_stalled"
    assert failure["retryable"] is False
    assert failure["submission_state"] == "post_submission"
    assert failure["failure_class"] == "control_invariant_failure"
    assert failure["details"]["reason"] == "active_signal_stalled"
    assert "q2_provider_response_missing" not in str(result)
    assert [call.metadata["source_id"] for call in adapter.calls] == ["S1"]
    assert model_uow.state[model_run_id].status is ModelRunStatus.NEEDS_REVIEW
    assert model_uow.state[model_run_id].error_code == "active_signal_stalled"


@pytest.mark.asyncio
async def test_q2_succeeded_empty_output_keeps_provider_response_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _Q2Gateway(None, output_text="")
    orchestrator, run, _ = _q2_orchestrator(monkeypatch, gateway, _q2_report(2))

    result = await orchestrator._execute_direct_url_extraction(run, snapshot=_q2_snapshot())

    assert result["status"] == "needs_review"
    assert result["error_code"] == "q2_provider_response_missing"
    assert result["source_failures"]["S1"]["failure_class"] == ("control_invariant_failure")
    assert gateway.calls == ["S1"]


@pytest.mark.asyncio
async def test_q2_bridge_unreachable_before_submit_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _PersistentQ2Adapter(
        first_error_code="bridge_unreachable", first_submission_state=None
    )
    gateway, model_uow = _persistent_q2_gateway(adapter)
    orchestrator, run, _ = _q2_orchestrator(monkeypatch, gateway, _q2_report(1))

    first = await orchestrator.execute_stage(
        run.id, SubjectProductionStage.EXTRACTION, correlation_id="test"
    )
    model_run_id = _q2_source_model_run_id(
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        source_id="S1",
        canonical_url="https://example.test/1",
    )
    assert model_uow.state[model_run_id].submission_state is ModelSubmissionState.NOT_SUBMITTED
    second = await orchestrator.execute_stage(
        run.id, SubjectProductionStage.EXTRACTION, correlation_id="test"
    )

    assert first["status"] == "transient_error"
    assert second["status"] == "success"
    assert [call.metadata["source_id"] for call in adapter.calls] == ["S1", "S1"]
    assert model_uow.state[model_run_id].status is ModelRunStatus.SUCCEEDED


async def test_q2_nonretryable_source_failure_keeps_source_coverage_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _Q2Gateway(
        BridgeTransportError(
            "source_content_invalid",
            "source-specific response is unusable",
            retryable=False,
            phase="model_call",
            submission_state="post_submission",
        )
    )
    # The fake gateway fails only S1; S2 must still be requested and parsed.
    original_execute = gateway.execute

    async def execute(request: ModelRequest, role: ModelRole) -> object:
        if request.metadata["source_id"] != "S1":
            gateway.failure = None
        return await original_execute(request, role)

    gateway.execute = execute  # type: ignore[method-assign]
    orchestrator, run, _ = _q2_orchestrator(monkeypatch, gateway, _q2_report(2))

    result = await orchestrator._execute_direct_url_extraction(run, snapshot=_q2_snapshot())

    assert gateway.calls == ["S1", "S2"]
    assert result["status"] == "needs_review"
    assert result["error_code"] == "q2_source_coverage_failed"
    assert result["completed_source_ids"] == ["S2"]
    assert result["failed_source_ids"] == ["S1"]
    assert result["details"]["source_failures"]["S1"]["error_code"] == "source_content_invalid"
    assert result["details"]["source_failures"]["S1"]["failure_class"] == ("source_content_failure")
    assert orchestrator._extraction.store_calls == []

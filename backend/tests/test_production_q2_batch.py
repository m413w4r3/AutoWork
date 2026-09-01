from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_q2_batch, production_workflow
from cti_app.application.model_gateway import (
    ModelGateway,
    ModelRequest,
    ModelRouter,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_artifact_verification import (
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_parsers import (
    ParsedSource,
    Q2ArtifactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
    ReferenceReport,
    q2_source_output_to_json,
)
from cti_app.application.production_prompts import (
    IOC_RULES_BATCH_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.application.production_source_evidence import (
    verify_ioc_rules_output_against_source,
)
from cti_app.application.production_workflow import (
    _q2_batch_model_run_id,
    _source_extraction_model_run_id,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import ModelRole, ModelRunStatus
from cti_app.domain.production import (
    ExtractionProfile,
    SourceExtraction,
    SourceExtractionStatus,
    SubjectProductionRun,
    SubjectProductionStage,
)
from cti_app.integrations.models import FakeModelAdapter, InMemoryModelOutputStore
from tests.model_support import InMemoryModelRunUnitOfWorkFactory
from tests.test_production_extraction_profiles import (
    _archived_document,
    _ArchivedBlobs,
    _cached_orchestrator,
    _CacheState,
    _CacheStore,
    _collection_for,
    _ExtractionSink,
    _input_source,
    _snapshot,
)


def _source(index: int) -> ParsedSource:
    url = f"https://example.test/source-{index}"
    return ParsedSource(
        local_id=f"S{index}",
        title=f"Source {index}",
        url=url,
        canonical_url=url,
        publisher="Publisher",
        published_at=date(2025, 1, index),
        role=SourceRole.INDEPENDENT,
    )


def _candidate(index: int, text: str = "archived") -> production_q2_batch.Q2BatchCandidate:
    return production_q2_batch.Q2BatchCandidate(
        source=_source(index),
        archived_text=text,
        source_content_sha256=f"{index:064x}",
    )


def _batch_response(*source_outputs: str) -> str:
    return "\n\n".join(
        f"SOURCE B{index}\n{output}" for index, output in enumerate(source_outputs, start=1)
    )


def test_batch_partition_is_greedy_by_report_order_and_limits_sources() -> None:
    nine = production_q2_batch.partition_q2_batch_candidates(
        tuple(_candidate(index) for index in range(1, 10))
    )
    assert [len(batch) for batch in nine] == [8, 1]
    assert [item.source.local_id for item in nine[0]] == [f"S{index}" for index in range(1, 9)]

    by_chars = production_q2_batch.partition_q2_batch_candidates(
        (_candidate(1, "a" * 35_000), _candidate(2, "b" * 35_000), _candidate(3, "c"))
    )
    assert [len(batch) for batch in by_chars] == [2, 1]
    assert sum(len(item.archived_text) for item in by_chars[0]) == 70_000


def test_batch_candidate_over_budget_is_not_silently_truncated() -> None:
    oversized = _candidate(1, "x" * (production_q2_batch.MAX_Q2_BATCH_ARCHIVED_CHARS + 1))
    with pytest.raises(ValueError):
        production_q2_batch.partition_q2_batch_candidates((oversized,))


def test_batch_parser_handles_terminal_states_missing_duplicates_unknowns_and_fences() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in range(1, 4)))
    response = """SOURCE B1
RULE yara: embedded
```yara
rule embedded {
  strings:
    $x = "SOURCE B2"
  condition:
    $x
}
```
SOURCE B1
EMPTY
SOURCE B9
EMPTY
"""
    parsed = production_q2_batch.parse_q2_batch_response(response, batch.sources)

    assert parsed.usable
    assert parsed.warnings.count("batch_source_unknown") == 1
    by_id = {item.batch_id: item for item in parsed.sources}
    assert by_id["B1"].error_code == "batch_source_duplicate"
    assert by_id["B2"].error_code == "batch_source_missing"
    assert by_id["B3"].error_code == "batch_source_missing"

    terminals = production_q2_batch.parse_q2_batch_response(
        "SOURCE B1\nEMPTY\nSOURCE B2\nUNAVAILABLE\nSOURCE B3\nEMPTY",
        batch.sources,
    )
    assert [item.status for item in terminals.sources] == [
        "succeeded",
        "failed",
        "succeeded",
    ]
    assert terminals.sources[1].error_code == "batch_source_unavailable"


def test_batch_parser_does_not_split_source_header_inside_rule_fence() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in (1, 2)))
    parsed = production_q2_batch.parse_q2_batch_response(
        _batch_response(
            """RULE yara: embedded
```yara
rule embedded {
  strings:
    $x = "SOURCE B2"
  condition:
    $x
}
```""",
            "EMPTY",
        ),
        batch.sources,
    )
    assert parsed.usable
    assert len(parsed.sources[0].output.rules) == 1  # type: ignore[union-attr]
    assert parsed.sources[1].output is not None


def test_batch_prompt_is_archive_only_and_uses_only_batch_ids() -> None:
    prompt = ProductionPromptTemplates.get_ioc_rules_batch_prompt(
        [("B1", "exact archive one"), ("B2", "exact archive two")]
    )
    assert "<Q2_SOURCE B1>\nexact archive one\n</Q2_SOURCE>" in prompt
    assert "<Q2_SOURCE B2>\nexact archive two\n</Q2_SOURCE>" in prompt
    assert "S1" not in prompt
    assert " :: " not in prompt
    assert "independently" in prompt
    assert "<literal body>\n```\n\nSOURCE B2" in prompt
    assert IOC_RULES_BATCH_PROMPT_VERSION == "2"


def test_batch_model_run_id_is_content_order_and_version_addressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = ("a" * 64, "b" * 64)
    first = _q2_batch_model_run_id(source_content_sha256=hashes)
    assert first == _q2_batch_model_run_id(source_content_sha256=hashes)
    assert first != _q2_batch_model_run_id(source_content_sha256=hashes[::-1])
    assert first != _q2_batch_model_run_id(source_content_sha256=("a" * 64, "c" * 64))
    monkeypatch.setattr(production_workflow, "Q2_BATCH_PARSER_VERSION", "next")
    assert first != _q2_batch_model_run_id(source_content_sha256=hashes)


def test_batch_local_evidence_gate_rejects_cross_source_iocs_and_rules() -> None:
    output = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                artifact_type="domain",
                value="b2.example.org",
                indicator_status="confirmed_ioc",
            )
        ],
        rules=[
            Q2RuleProposal(
                rule_type="yara",
                name="wrong",
                body="rule wrong { condition: true }",
            )
        ],
    )
    gated = verify_ioc_rules_output_against_source(
        output,
        "The source contains b1.example.org and no rule named wrong.",
    )
    assert gated.filtered_output.artifacts == []
    assert gated.filtered_output.rules == []
    assert len(gated.rejections) == 2


def test_global_verifier_keeps_provenance_from_duplicate_ioc_submissions() -> None:
    output = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                artifact_type="domain",
                value="shared.security-lab.io",
                indicator_status="confirmed_ioc",
            )
        ]
    )
    verification = verify_q2_proposals(
        [
            Q2ProposalSubmission(output=output, source_ids=("S1",), model_run_id="batch"),
            Q2ProposalSubmission(output=output, source_ids=("S3",), model_run_id="batch"),
        ]
    )
    assert verification.canonical.items[0].source_ids == ("S1", "S3")


class _BatchGateway:
    def __init__(
        self, responses: list[str] | None = None, failure: Exception | None = None
    ) -> None:
        self.responses = responses or []
        self.failure = failure
        self.calls: list[ModelRequest] = []

    async def execute(self, request: ModelRequest, role: ModelRole) -> object:
        del role
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        response = self.responses.pop(0)
        return SimpleNamespace(
            output_text=response,
            run=SimpleNamespace(
                id=request.run_id,
                status=ModelRunStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                error_details=None,
            ),
            metadata={},
        )


def _batch_workflow(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    response: str,
    *,
    gateway: _BatchGateway | None = None,
    archived_texts: list[str] | None = None,
) -> tuple[object, SubjectProductionRun, _CacheState, _ExtractionSink, _BatchGateway]:
    subject = uuid4()
    blobs = _ArchivedBlobs()
    documents: dict[UUID, SimpleNamespace] = {}
    collections: dict[UUID, SimpleNamespace] = {}
    for index in range(1, count + 1):
        source = _source(index)
        archived_text = (
            archived_texts[index - 1]
            if archived_texts is not None
            else f"IOC {index}: domain-{index}.security-lab.io"
        )
        document = _archived_document(
            blobs,
            subject_id=subject,
            url=source.canonical_url,
            content=archived_text.encode(),
        )
        documents[document.id] = document
        collections[uuid4()] = _collection_for(document, source.canonical_url)
    state = _CacheState(documents, collections)
    sink = _ExtractionSink()
    actual_gateway = gateway or _BatchGateway([response])
    orchestrator = _cached_orchestrator(
        state,
        actual_gateway,  # type: ignore[arg-type]
        _CacheStore(),
        sink,
        monkeypatch,
        blobs,
    )
    report = ReferenceReport(
        sources=tuple(_source(index) for index in range(1, count + 1)), events=()
    )

    async def load_report(*args: object) -> ReferenceReport:
        del args
        return report

    orchestrator._load_reference_report = load_report
    run = SubjectProductionRun(
        subject_id=subject,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[run.id] = run
    return orchestrator, run, state, sink, actual_gateway


@pytest.mark.asyncio
async def test_five_archived_light_misses_use_one_batch_without_source_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _batch_response(
        *(f"IOC confirmed domain\n- domain-{index}.security-lab.io" for index in range(1, 6))
    )
    orchestrator, run, state, sink, gateway = _batch_workflow(monkeypatch, 5, response)

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert len(gateway.calls) == 1
    assert gateway.calls[0].prompt_template_id == "production-q2-ioc-batch"
    assert result["model_calls"] == 1
    assert result["light_calls"] == 1
    assert result["light_batches"] == 1
    assert result["light_sources_batched"] == 5
    assert state.extractions.rows == {}
    assert run.extraction_progress["model_calls"] == 1  # type: ignore[index]
    assert len(sink.calls[-1]["canonical_json"]["items"]) == 5  # type: ignore[index]


@pytest.mark.asyncio
async def test_batch_partial_source_failure_keeps_safe_sources_and_marks_coverage_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _batch_response(
        "IOC confirmed domain\n- domain-1.security-lab.io",
        "UNAVAILABLE",
        "IOC confirmed domain\n- domain-3.security-lab.io",
    )
    orchestrator, run, state, sink, gateway = _batch_workflow(monkeypatch, 3, response)

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "needs_review"
    assert result["error_code"] == "q2_source_coverage_failed"
    assert result["completed_source_ids"] == ["S1", "S3"]
    assert result["source_failures"]["S2"]["error_code"] == "batch_source_unavailable"
    assert len(gateway.calls) == 1
    assert state.extractions.rows == {}
    items = sink.calls  # No extraction artifact is stored on coverage failure.
    assert items == []


@pytest.mark.asyncio
async def test_single_light_candidate_uses_individual_checkpoint_and_never_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = "IOC confirmed domain\n- domain-1.security-lab.io"
    orchestrator, run, state, _, gateway = _batch_workflow(monkeypatch, 1, response)
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )
    assert result["status"] == "success"
    assert result["light_calls"] == 1
    assert result["light_batches"] == 0
    assert gateway.calls[0].prompt_template_id == "production-q2-url"
    assert len(state.extractions.rows) == 1
    assert next(iter(state.extractions.rows.values())).status is SourceExtractionStatus.VERIFIED


@pytest.mark.asyncio
async def test_light_source_over_batch_budget_uses_exact_archive_individually(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = "domain-1.security-lab.io\n" + "x" * (production_q2_batch.MAX_Q2_BATCH_ARCHIVED_CHARS)
    orchestrator, run, state, _, gateway = _batch_workflow(
        monkeypatch,
        1,
        "IOC confirmed domain\n- domain-1.security-lab.io",
        archived_texts=[archive],
    )
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )
    assert result["status"] == "success"
    assert result["light_batches"] == 0
    assert "x" * production_q2_batch.MAX_Q2_BATCH_ARCHIVED_CHARS in gateway.calls[0].text
    assert len(state.extractions.rows) == 1


@pytest.mark.asyncio
async def test_full_source_and_live_fallback_are_never_batched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_gateway = _BatchGateway(["FACT malware\n- ExampleRAT"])
    orchestrator, run, state, _, gateway = _batch_workflow(
        monkeypatch,
        1,
        "FACT malware\n- ExampleRAT",
        gateway=full_gateway,
    )
    source_url = "https://example.test/source-1"
    full_result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source(source_url, date(2026, 7, 10)),)),
    )
    assert full_result["status"] == "success"
    assert full_result["light_batches"] == 0
    assert gateway.calls[0].prompt_template_id == "production-q2-url"
    assert len(state.extractions.rows) == 1

    live_run = SubjectProductionRun(
        subject_id=run.subject_id,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[live_run.id] = live_run
    orchestrator._blob_reader.contents.clear()  # type: ignore[union-attr]
    full_gateway.responses.append("IOC confirmed domain\n- domain-1.security-lab.io")
    live_result = await orchestrator._execute_direct_url_extraction(
        live_run,
        snapshot=_snapshot((_input_source(source_url, date(2026, 7, 10)),)),
    )
    assert live_result["status"] == "success"
    assert live_result["light_batches"] == 0
    assert len(state.extractions.rows) == 1


@pytest.mark.asyncio
async def test_cache_hit_is_removed_before_batch_partition(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _batch_response(
        "IOC confirmed domain\n- domain-2.security-lab.io",
        "IOC confirmed domain\n- domain-3.security-lab.io",
    )
    orchestrator, run, state, _, gateway = _batch_workflow(monkeypatch, 3, response)
    source = _source(1)
    document = next(
        document for document in state._docs_by_id.values() if document.final_url == source.url
    )
    source_hash = document.decoded_sha256
    identity = orchestrator._source_extraction_identity(
        source_content_sha256=source_hash,
        profile=ExtractionProfile.IOC_RULES,
    )
    canonical_blob_id = uuid4()
    cached_run_id = _source_extraction_model_run_id(
        source_content_sha256=source_hash,
        profile=ExtractionProfile.IOC_RULES,
    )
    await state.extractions.save(
        SourceExtraction(
            canonical_url=source.canonical_url,
            source_content_sha256=source_hash,
            profile=ExtractionProfile.IOC_RULES,
            contract_version=identity["contract_version"],
            prompt_version=identity["prompt_version"],
            parser_version=identity["parser_version"],
            verifier_version=identity["verifier_version"],
            status=SourceExtractionStatus.VERIFIED,
            canonical_blob_id=canonical_blob_id,
            model_run_id=cached_run_id,
        )
    )
    orchestrator._artifact_store.payloads[canonical_blob_id] = q2_source_output_to_json(
        Q2SourceOutput()
    )
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )
    assert result["status"] == "success"
    assert result["cache_hits"] == 1
    assert result["light_batches"] == 1
    assert result["light_sources_batched"] == 2
    assert len(gateway.calls) == 1
    assert len(state.extractions.rows) == 1


@pytest.mark.asyncio
async def test_cache_hit_can_reduce_candidates_to_singleton_individual_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = "IOC confirmed domain\n- domain-2.security-lab.io"
    orchestrator, run, state, _, gateway = _batch_workflow(monkeypatch, 2, response)
    source = _source(1)
    document = next(
        document for document in state._docs_by_id.values() if document.final_url == source.url
    )
    source_hash = document.decoded_sha256
    identity = orchestrator._source_extraction_identity(
        source_content_sha256=source_hash,
        profile=ExtractionProfile.IOC_RULES,
    )
    canonical_blob_id = uuid4()
    await state.extractions.save(
        SourceExtraction(
            canonical_url=source.canonical_url,
            source_content_sha256=source_hash,
            profile=ExtractionProfile.IOC_RULES,
            contract_version=identity["contract_version"],
            prompt_version=identity["prompt_version"],
            parser_version=identity["parser_version"],
            verifier_version=identity["verifier_version"],
            status=SourceExtractionStatus.VERIFIED,
            canonical_blob_id=canonical_blob_id,
            model_run_id=uuid4(),
        )
    )
    orchestrator._artifact_store.payloads[canonical_blob_id] = q2_source_output_to_json(
        Q2SourceOutput()
    )
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )
    assert result["status"] == "success"
    assert result["cache_hits"] == 1
    assert result["light_batches"] == 0
    assert gateway.calls[0].prompt_template_id == "production-q2-url"
    assert len(state.extractions.rows) == 2


@pytest.mark.asyncio
async def test_batch_transport_failure_is_global_and_does_not_create_source_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.integrations.models import BridgeTransportError

    gateway = _BatchGateway(
        failure=BridgeTransportError(
            "bridge_ui_timeout",
            "transport failure",
            retryable=True,
            phase="pre_submission",
            submission_state="pre_submission",
        )
    )
    orchestrator, run, state, _, _ = _batch_workflow(
        monkeypatch,
        3,
        "",
        gateway=gateway,
    )
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )
    assert result["status"] == "transient_error"
    assert result["error_code"] == "bridge_ui_timeout"
    assert result["source_failures"] == {}
    assert state.extractions.rows == {}


@pytest.mark.asyncio
async def test_batch_reconciliation_failure_is_global_and_not_resubmitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _BatchGateway(failure=ModelSubmissionReconciliationRequiredError())
    orchestrator, run, state, _, _ = _batch_workflow(
        monkeypatch,
        3,
        "",
        gateway=gateway,
    )
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )
    assert result["status"] == "needs_review"
    assert result["error_code"] == "model_submission_reconciliation_required"
    assert result["source_failures"] == {}
    assert state.extractions.rows == {}
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_exact_batch_reuses_durable_model_run_without_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _batch_response(
        "IOC confirmed domain\n- domain-1.security-lab.io",
        "IOC confirmed domain\n- domain-2.security-lab.io",
    )
    adapter = FakeModelAdapter(research_text=response)
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
    orchestrator, run, state, _, _ = _batch_workflow(
        monkeypatch,
        2,
        response,
        gateway=gateway,  # type: ignore[arg-type]
    )
    snapshot = _snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),))
    first = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)
    replay = SubjectProductionRun(
        subject_id=run.subject_id,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[replay.id] = replay
    second = await orchestrator._execute_direct_url_extraction(replay, snapshot=snapshot)

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert len(adapter.calls) == 1
    assert len(model_uow.state) == 1
    assert second["model_calls"] == 0
    assert second["light_calls"] == 0
    assert second["light_batches"] == 0
    assert state.extractions.rows == {}

from __future__ import annotations

import hashlib
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application import (
    production_parsers,
    production_prompts,
    production_q2_batch,
    production_workflow,
)
from cti_app.application.model_gateway import (
    ModelGateway,
    ModelRequest,
    ModelRouter,
    ModelRoutingHint,
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
    SOURCE_EVIDENCE_VERSION,
    verify_ioc_rules_output_against_source,
)
from cti_app.application.production_workflow import (
    _q2_batch_model_run_id,
    _source_extraction_model_run_id,
    _source_extraction_verifier_identity,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRunStatus
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


def _block(batch_id: str, body: str) -> str:
    marker = production_q2_batch.q2_batch_output_marker(batch_id)
    return f"{marker}\n{body}"


def _batch_response(*source_outputs: str) -> str:
    return "\n\n".join(
        _block(f"B{index}", output) for index, output in enumerate(source_outputs, start=1)
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


def test_batch_parser_reads_expected_markers_and_terminal_states() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in range(1, 4)))
    parsed = production_q2_batch.parse_q2_batch_response(
        _batch_response(
            "IOC confirmed domain\n- evil.example",
            "EMPTY",
            "UNAVAILABLE",
        ),
        batch.sources,
    )
    assert parsed.usable
    assert parsed.warnings == ()
    assert [item.status for item in parsed.sources] == ["succeeded", "succeeded", "failed"]
    assert parsed.sources[0].output is not None
    assert parsed.sources[0].output.artifacts[0].value == "evil.example"
    assert parsed.sources[1].output == Q2SourceOutput()
    assert parsed.sources[2].error_code == "batch_source_unavailable"


def test_batch_parser_accepts_lowercase_markers_and_terminal_states_case_insensitively() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in (1, 2)))
    parsed = production_q2_batch.parse_q2_batch_response(
        "  @@q2:b01@@  \n eMpTy \n\t@@Q2:B2@@\n UnAvAiLaBlE \n",
        batch.sources,
    )

    assert parsed.usable
    assert [item.batch_id for item in parsed.sources] == ["B1", "B2"]
    assert parsed.sources[0].output == Q2SourceOutput()
    assert parsed.sources[1].error_code == "batch_source_unavailable"


def test_empty_expected_block_is_invalid() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in (1, 2)))
    parsed = production_q2_batch.parse_q2_batch_response(
        "@@Q2:B1@@\n\n@@Q2:B2@@\nEMPTY", batch.sources
    )

    assert parsed.usable
    assert parsed.sources[0].error_code == "batch_source_invalid"
    assert parsed.sources[1].output == Q2SourceOutput()


def test_batch_parser_reports_missing_duplicate_and_unknown_sources() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in range(1, 4)))
    response = "\n".join(
        (
            _block("B1", "EMPTY"),
            _block("B1", "IOC confirmed domain\n- evil.example"),
            _block("B9", "EMPTY"),
        )
    )
    parsed = production_q2_batch.parse_q2_batch_response(response, batch.sources)

    assert parsed.usable
    assert parsed.warnings.count("batch_source_unknown") == 1
    by_id = {item.batch_id: item for item in parsed.sources}
    assert by_id["B1"].error_code == "batch_source_duplicate"
    assert by_id["B1"].output is None
    assert by_id["B2"].error_code == "batch_source_missing"
    assert by_id["B3"].error_code == "batch_source_missing"


def test_unknown_marker_content_is_not_attributed_to_an_expected_source() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in (1, 2)))
    parsed = production_q2_batch.parse_q2_batch_response(
        "@@Q2:B9@@\nIOC confirmed domain\n- unknown.example\n@@Q2:B2@@\nEMPTY",
        batch.sources,
    )

    assert parsed.usable
    assert parsed.warnings.count("batch_source_unknown") == 1
    assert parsed.sources[0].error_code == "batch_source_missing"
    assert parsed.sources[1].output == Q2SourceOutput()


def test_next_marker_closes_previous_source_and_eof_closes_last_source() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in (1, 2)))
    begin = production_q2_batch.q2_batch_output_marker("B1")
    response = "\n".join(
        (
            begin,
            "IOC confirmed domain\n- b1.example",
            _block("B2", "IOC confirmed domain\n- b2.example"),
        )
    )
    parsed = production_q2_batch.parse_q2_batch_response(response, batch.sources)

    assert parsed.usable
    assert parsed.sources[0].status == "succeeded"
    assert parsed.sources[0].output is not None
    assert parsed.sources[0].output.artifacts[0].value == "b1.example"
    assert parsed.sources[1].status == "succeeded"
    assert parsed.sources[1].output is not None
    assert parsed.sources[1].output.artifacts[0].value == "b2.example"


def test_unclosed_rule_fence_in_first_block_still_recovers_the_next_block() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in (1, 2)))
    begin = production_q2_batch.q2_batch_output_marker("B1")
    response = "\n".join(
        (
            begin,
            "RULE yara: broken",
            "```yara",
            "rule broken {",
            "  condition:",
            "    true",
            _block("B2", "IOC confirmed domain\n- b2.example"),
        )
    )
    parsed = production_q2_batch.parse_q2_batch_response(response, batch.sources)

    assert parsed.usable
    assert parsed.sources[0].status == "failed"
    assert parsed.sources[0].error_code == "batch_source_invalid"
    assert parsed.sources[1].status == "succeeded"
    assert parsed.sources[1].output is not None
    assert parsed.sources[1].output.artifacts[0].value == "b2.example"


def test_closed_rule_fence_is_parsed_and_next_block_is_independent() -> None:
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
            "IOC confirmed domain\n- b2.example",
        ),
        batch.sources,
    )
    assert parsed.usable
    assert len(parsed.sources[0].output.rules) == 1  # type: ignore[union-attr]
    assert parsed.sources[1].output is not None
    assert parsed.sources[1].output.artifacts[0].value == "b2.example"


def test_source_header_text_inside_a_rule_has_no_structural_meaning() -> None:
    batch = production_q2_batch.make_q2_batch(tuple(_candidate(index) for index in (1, 2)))
    parsed = production_q2_batch.parse_q2_batch_response(
        _batch_response(
            'RULE yara: embedded\n```yara\nrule embedded {\n  strings:\n    $x = "SOURCE B2"\n'
            "  condition:\n    $x\n}\n```\nSOURCE B2\nIOC confirmed domain\n- still-b1.example",
            "EMPTY",
        ),
        batch.sources,
    )
    assert parsed.usable
    first = parsed.sources[0].output
    assert first is not None
    # The bare header text stays inside B1: it split nothing.
    assert [artifact.value for artifact in first.artifacts] == ["still-b1.example"]
    assert parsed.sources[1].output == Q2SourceOutput()


def test_batch_framing_markers_are_exact_and_collisions_use_whole_markers() -> None:
    batch_ids = ("B1", "B2")
    assert production_q2_batch.q2_batch_framing_markers(batch_ids) == (
        "@@Q2:B1@@",
        "@@Q2:B2@@",
        "@@Q2IN:B1@@",
        "@@Q2IN:B2@@",
    )
    assert not production_q2_batch.q2_batch_framing_collides(
        batch_ids, ("archive mentions B1 and Q2",)
    )
    assert production_q2_batch.q2_batch_framing_collides(
        batch_ids, ("archive contains @@Q2IN:B2@@ exactly",)
    )


def test_batch_prompt_is_archive_only_and_frames_inputs_with_minimal_markers() -> None:
    prompt = ProductionPromptTemplates.get_ioc_rules_batch_prompt(
        [("B1", "exact archive one"), ("B2", "exact archive two")]
    )
    for batch_id, text in (("B1", "exact archive one"), ("B2", "exact archive two")):
        marker = production_q2_batch.q2_batch_input_marker(batch_id)
        assert f"{marker}\n{text}" in prompt
        assert f"{marker}\n{text}\n@@" not in prompt
        output_marker = production_q2_batch.q2_batch_output_marker(batch_id)
        assert prompt.count(output_marker) == 1
    assert "@@Q2:B3@@" not in prompt
    assert "0123456789abcdef" not in prompt
    assert "SOURCE B" not in prompt
    assert "Q2_SOURCE" not in prompt
    assert "S1" not in prompt
    assert " :: " not in prompt
    assert "independently" in prompt
    assert IOC_RULES_BATCH_PROMPT_VERSION == "4"
    assert production_q2_batch.Q2_BATCH_PARSER_VERSION == "q2-batch-v3"


def test_batch_prompt_renders_exactly_the_real_eight_source_output_structure() -> None:
    prompt = ProductionPromptTemplates.get_ioc_rules_batch_prompt(
        [(f"B{index}", f"exact archive {index}") for index in range(1, 9)]
    )

    for index in range(1, 9):
        assert prompt.count(f"@@Q2:B{index}@@") == 1
        assert prompt.count(f"@@Q2IN:B{index}@@") == 1
    assert "@@Q2:B9@@" not in prompt
    assert "BEGIN" not in prompt
    assert ":END@@" not in prompt


def test_batch_model_run_id_is_content_order_and_version_addressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = ("a" * 64, "b" * 64)
    first = _q2_batch_model_run_id(source_content_sha256=hashes)
    assert first == _q2_batch_model_run_id(source_content_sha256=hashes)
    assert first != _q2_batch_model_run_id(source_content_sha256=hashes[::-1])
    assert first != _q2_batch_model_run_id(source_content_sha256=("a" * 64, "c" * 64))
    for name in (
        "Q2_EXTRACTION_CONTRACT_VERSION",
        "IOC_RULES_BATCH_PROMPT_VERSION",
        "Q2_MARKDOWN_PARSER_VERSION",
        "Q2_BATCH_PARSER_VERSION",
        "SOURCE_EVIDENCE_VERSION",
        "ARTIFACT_VERIFIER_VERSION",
        "Q2_ROUTING_POLICY_VERSION",
    ):
        with monkeypatch.context() as patched:
            patched.setattr(production_workflow, name, "next")
            assert first != _q2_batch_model_run_id(source_content_sha256=hashes), name


def test_batch_identity_ignores_q1_parser_and_individual_ioc_rules_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = ("a" * 64, "b" * 64)
    first = _q2_batch_model_run_id(source_content_sha256=hashes)
    monkeypatch.setattr(production_workflow, "PARSER_VERSION", "q1-next")
    monkeypatch.setattr(production_parsers, "PARSER_VERSION", "q1-next")
    monkeypatch.setattr(production_prompts, "IOC_RULES_PROMPT_VERSION", "999")
    assert _q2_batch_model_run_id(source_content_sha256=hashes) == first


def test_source_evidence_version_only_changes_source_checkpoint_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _source_extraction_verifier_identity(ExtractionProfile.FULL)
    source_hash = "a" * 64
    source_identity = (
        production_workflow.ProductionWorkflowOrchestrator._source_extraction_identity(
            source_content_sha256=source_hash,
            profile=ExtractionProfile.FULL,
        )
    )
    model_run_id = _source_extraction_model_run_id(
        source_content_sha256=source_hash,
        profile=ExtractionProfile.FULL,
    )

    monkeypatch.setattr(production_workflow, "SOURCE_EVIDENCE_VERSION", "next")

    assert _source_extraction_verifier_identity(ExtractionProfile.FULL) != first
    assert (
        production_workflow.ProductionWorkflowOrchestrator._source_extraction_identity(
            source_content_sha256=source_hash,
            profile=ExtractionProfile.FULL,
        )["verifier_version"]
        != source_identity["verifier_version"]
    )
    assert (
        _source_extraction_model_run_id(
            source_content_sha256=source_hash,
            profile=ExtractionProfile.FULL,
        )
        == model_run_id
    )


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
async def test_batch_marker_colliding_with_an_archive_falls_back_to_individual_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _BatchGateway(
        [
            "IOC confirmed domain\n- domain-1.security-lab.io",
            "IOC confirmed domain\n- domain-2.security-lab.io",
        ]
    )
    orchestrator, run, state, _, _ = _batch_workflow(
        monkeypatch,
        2,
        "",
        gateway=gateway,
        archived_texts=[
            "domain-1.security-lab.io leaked marker @@Q2:B1@@",
            "domain-2.security-lab.io",
        ],
    )
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert result["light_batches"] == 0
    assert result["light_sources_batched"] == 0
    assert len(gateway.calls) == 2
    assert {call.prompt_template_id for call in gateway.calls} == {"production-q2-url"}
    # The colliding archive is never rewritten, and both sources keep their own
    # individual checkpoint.
    assert len(state.extractions.rows) == 2


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
async def test_individual_archived_ioc_is_source_gated_before_checkpoint_and_canonical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, _state, sink, gateway = _batch_workflow(
        monkeypatch,
        1,
        "IOC confirmed domain\n- absent.example",
        archived_texts=["The archive contains present.example only."],
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert len(gateway.calls) == 1
    assert sink.calls[-1]["canonical_json"]["items"] == []  # type: ignore[index]
    canonical_payload = next(
        payload
        for payload in orchestrator._artifact_store.payloads.values()  # type: ignore[union-attr]
        if "artifacts" in payload
    )
    assert canonical_payload["artifacts"] == []
    assert "q2_source:S1:source_evidence_missing" in sink.calls[-1]["warnings"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_individual_archived_full_preserves_facts_and_gates_iocs_and_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = (
        "FACT malware\n- ExampleRAT\n"
        "IOC confirmed domain\n- absent.example\n"
        "RULE sigma: kept\n```sigma\n"
        "title: Kept\nlogsource:\n  product: windows\n```\n"
    )
    orchestrator, run, _state, sink, gateway = _batch_workflow(
        monkeypatch,
        1,
        response,
        archived_texts=["ExampleRAT\ntitle: Kept\nlogsource:\n  product: windows"],
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/source-1", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert len(gateway.calls) == 1
    canonical = sink.calls[-1]["canonical_json"]  # type: ignore[index]
    assert [item["value"] for item in canonical["items"]] == ["ExampleRAT"]
    assert len(canonical["rules"]) == 1
    assert "q2_source:S1:source_evidence_missing" in sink.calls[-1]["warnings"]  # type: ignore[operator]


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
async def test_cached_full_projection_is_gated_against_current_archived_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, state, sink, gateway = _batch_workflow(
        monkeypatch,
        1,
        "",
        archived_texts=["present.example"],
    )
    source = _source(1)
    document = next(
        document for document in state._docs_by_id.values() if document.final_url == source.url
    )
    source_hash = document.decoded_sha256
    identity = orchestrator._source_extraction_identity(
        source_content_sha256=source_hash,
        profile=ExtractionProfile.FULL,
    )
    canonical_blob_id = uuid4()
    await state.extractions.save(
        SourceExtraction(
            canonical_url=source.canonical_url,
            source_content_sha256=source_hash,
            profile=ExtractionProfile.FULL,
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
        Q2SourceOutput(
            artifacts=[
                Q2ArtifactProposal(
                    value="absent.example",
                    artifact_type="domain",
                    indicator_status="confirmed_ioc",
                )
            ]
        )
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert gateway.calls == []
    assert sink.calls[-1]["canonical_json"]["items"] == []  # type: ignore[index]
    assert "q2_source:S1:source_evidence_missing" in sink.calls[-1]["warnings"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_source_checkpoint_rebuilds_from_succeeded_model_run_under_current_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verifier bump reparses the durable ModelRun without a provider call."""
    profile = ExtractionProfile.IOC_RULES
    source_url = "https://example.test/source-1"
    archived_text = "The exact archive contains present.security-lab.io only."
    raw_response = (
        "IOC confirmed domain\n"
        "- present.security-lab.io\n"
        "IOC confirmed domain\n"
        "- absent.security-lab.io\n"
    )
    adapter = FakeModelAdapter(research_text=raw_response)
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    model_output_store = InMemoryModelOutputStore()
    model_gateway = ModelGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
        ),
        model_uow,
        model_output_store,
    )
    orchestrator, run, state, sink, _ = _batch_workflow(
        monkeypatch,
        1,
        "",
        gateway=model_gateway,  # type: ignore[arg-type]
        archived_texts=[archived_text],
    )

    source = _source(1)
    assert source.canonical_url == source_url
    document = next(
        document for document in state._docs_by_id.values() if document.final_url == source_url
    )
    source_content_sha256 = document.decoded_sha256
    model_run_id = _source_extraction_model_run_id(
        source_content_sha256=source_content_sha256,
        profile=profile,
    )
    prompt = ProductionPromptTemplates.get_extraction_prompt(
        "Subject",
        source.local_id,
        source.title,
        source.canonical_url,
        profile=profile,
        archived_source_content=archived_text,
    )
    seeded = await model_gateway.execute(
        ModelRequest(
            text=prompt,
            prompt_template_id="production-q2-url",
            prompt_template_version=production_workflow.EXTRACTION_PROMPT_VERSION_BY_PROFILE[
                profile
            ],
            evidence_pack_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            external_llm_allowed=True,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            provider=ModelProvider.OPENAI,
            web_search=True,
            run_id=model_run_id,
            allow_failed_resubmit=True,
            metadata={
                "source_id": source.local_id,
                "source_url": source.canonical_url,
                "profile": profile.value,
                "source_content_sha256": source_content_sha256,
                "extraction_contract_version": production_workflow.Q2_EXTRACTION_CONTRACT_VERSION,
                "parser_version": production_workflow.Q2_MARKDOWN_PARSER_VERSION,
                "verifier_version": production_workflow.ARTIFACT_VERIFIER_VERSION,
            },
        ),
        ModelRole.RESEARCH,
    )
    assert seeded.run.id == model_run_id
    assert seeded.run.status is ModelRunStatus.SUCCEEDED
    assert state.extractions.rows == {}
    adapter.calls.clear()

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert result["model_calls"] == 0
    assert result["cache_hits"] == 0
    assert adapter.calls == []

    checkpoints = list(state.extractions.rows.values())
    assert len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint.status is SourceExtractionStatus.VERIFIED
    assert checkpoint.source_content_sha256 == source_content_sha256
    assert checkpoint.profile is profile
    assert checkpoint.model_run_id == model_run_id
    identity = orchestrator._source_extraction_identity(
        source_content_sha256=source_content_sha256,
        profile=profile,
    )
    assert checkpoint.verifier_version == identity["verifier_version"]
    assert checkpoint.verifier_version == _source_extraction_verifier_identity(profile)
    assert f"source-evidence-{SOURCE_EVIDENCE_VERSION}" in checkpoint.verifier_version

    assert checkpoint.canonical_blob_id is not None
    canonical_payload = orchestrator._artifact_store.payloads[  # type: ignore[union-attr]
        checkpoint.canonical_blob_id
    ]
    assert [item["value"] for item in canonical_payload["artifacts"]] == ["present.security-lab.io"]
    assert sink.calls[-1]["canonical_json"]["items"]  # type: ignore[index]
    assert "q2_source:S1:source_evidence_missing" in sink.calls[-1]["warnings"]  # type: ignore[operator]


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

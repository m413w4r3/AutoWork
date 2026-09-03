from __future__ import annotations

from copy import deepcopy
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
    Q2SourceOutput,
    ReferenceReport,
)
from cti_app.application.production_prompts import (
    IOC_RULES_BATCH_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.application.production_workflow import _q2_batch_model_run_id
from cti_app.domain.discovery import SourceRole
from cti_app.domain.model_runs import ModelRole, ModelRunStatus
from cti_app.domain.production import (
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


def _candidate(index: int) -> production_q2_batch.Q2BatchCandidate:
    return production_q2_batch.Q2BatchCandidate(source=_source(index))


def _block(batch_id: str, body: str) -> str:
    marker = production_q2_batch.q2_batch_output_marker(batch_id)
    return f"{marker}\n{body}"


def _batch_response(*source_outputs: str) -> str:
    return "\n\n".join(
        _block(f"B{index}", output) for index, output in enumerate(source_outputs, start=1)
    )


def test_batch_partition_keeps_report_order_and_limits_sources() -> None:
    nine = production_q2_batch.partition_q2_batch_candidates(
        tuple(_candidate(index) for index in range(1, 10))
    )
    assert [len(batch) for batch in nine] == [4, 4, 1]
    assert [item.source.local_id for item in nine[0]] == [f"S{index}" for index in range(1, 5)]
    assert production_q2_batch.MAX_Q2_BATCH_SOURCES == 4


def test_a_source_without_an_http_url_is_never_a_batch_candidate() -> None:
    without_url = _source(1).__class__(
        local_id="S1",
        title="Source 1",
        url="",
        canonical_url="",
        publisher="Publisher",
        published_at=date(2025, 1, 1),
        role=SourceRole.INDEPENDENT,
    )
    with pytest.raises(ValueError):
        production_q2_batch.Q2BatchCandidate(source=without_url)


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


def test_batch_prompt_lists_exact_urls_and_frames_only_the_output() -> None:
    prompt = ProductionPromptTemplates.get_ioc_rules_batch_prompt(
        "RedKitten",
        [
            ("B1", "https://vendor.example/report"),
            ("B2", "https://cert.example/advisory"),
        ],
    )
    assert "B1 https://vendor.example/report" in prompt
    assert "B2 https://cert.example/advisory" in prompt
    for batch_id in ("B1", "B2"):
        assert prompt.count(production_q2_batch.q2_batch_output_marker(batch_id)) == 1
    assert "@@Q2:B3@@" not in prompt
    assert "@@Q2IN" not in prompt
    assert "Open every exact source URL" in prompt
    assert "images/screenshots" in prompt
    assert "Do not follow a link to another publication, IOC page, repository" in prompt
    assert "independently" in prompt
    prompt_on_one_line = " ".join(prompt.split())
    assert (
        "Linked technical resources must be handled as distinct sources by Q1."
        in prompt_on_one_line
    )
    assert "Do not emit URLs, hashes" not in prompt_on_one_line
    assert "Do not repeat an input source URL as provenance." in prompt_on_one_line
    assert "Do not emit model ids, internal ids or internal content hashes." in prompt_on_one_line
    assert (
        "Extract URL, MD5, SHA1, SHA256 and SHA512 indicators only when they are actually"
        " published by that exact source URL; never follow a linked resource to obtain an"
        " indicator."
    ) in prompt_on_one_line
    assert (
        "Use `confirmed` when the publication explicitly presents a value as an IOC"
        in prompt_on_one_line
    )
    assert "Use `contextual` only when the technical value is relevant" in prompt_on_one_line
    assert "do not sample, summarize, collapse ranges" in prompt_on_one_line
    assert (
        "A single table cell may contain multiple IOC literals separated by whitespace"
        in prompt_on_one_line
    )
    for ioc_type in ("url", "md5", "sha1", "sha256", "sha512"):
        assert ioc_type in prompt
    assert " :: " not in prompt
    assert "S1" not in prompt
    assert IOC_RULES_BATCH_PROMPT_VERSION == "10"
    assert production_q2_batch.Q2_BATCH_PARSER_VERSION == "q2-batch-v3"


def test_batch_prompt_renders_exactly_the_real_eight_source_output_structure() -> None:
    prompt = ProductionPromptTemplates.get_ioc_rules_batch_prompt(
        "RedKitten",
        [(f"B{index}", f"https://example.test/source-{index}") for index in range(1, 9)],
    )

    for index in range(1, 9):
        assert prompt.count(f"@@Q2:B{index}@@") == 1
        assert prompt.count(f"B{index} https://example.test/source-{index}") == 1
    assert "@@Q2:B9@@" not in prompt
    assert "BEGIN" not in prompt
    assert ":END@@" not in prompt
    # The Subject is stated once for the whole batch, never per B# block.
    assert prompt.count("RedKitten") == 1
    assert prompt.count("**Subject**:") == 1
    assert prompt.index("**Subject**: RedKitten") < prompt.index("B1 https://example.test/source-1")


def test_batch_prompt_makes_the_subject_the_relevance_boundary_of_every_block() -> None:
    prompt = ProductionPromptTemplates.get_ioc_rules_batch_prompt(
        "RedKitten",
        [("B1", "https://vendor.example/multi-actor-report")],
    )
    one_line = " ".join(prompt.split())

    # Source independence must not be read as "no subject filtering".
    assert "Never use one publication to interpret or classify another." in one_line
    assert "Never move an IOC or rule between publications." in one_line
    assert "The Subject is the relevance boundary for every B#." in one_line
    assert "Source independence does not suspend subject filtering." in one_line
    assert "discard indicators/rules explicitly belonging to other activities" in one_line
    assert "exhaustively emit the remaining subject-relevant indicators/rules" in one_line
    # No unqualified "extract every IOC of the publication" instruction remains.
    assert "Extract every source-supported literal IOC" not in one_line
    assert "Extract every subject-relevant source-supported literal IOC" in one_line


def test_batch_model_run_id_is_run_url_and_version_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    urls = ("https://example.test/a", "https://example.test/b")
    identity = {"production_run_id": run_id, "pipeline_generation": 1, "canonical_urls": urls}
    first = _q2_batch_model_run_id(**identity)  # type: ignore[arg-type]
    assert first == _q2_batch_model_run_id(**identity)  # type: ignore[arg-type]
    assert first != _q2_batch_model_run_id(  # type: ignore[arg-type]
        **{**identity, "canonical_urls": urls[::-1]}
    )
    assert first != _q2_batch_model_run_id(  # type: ignore[arg-type]
        **{**identity, "canonical_urls": ("https://example.test/a", "https://example.test/c")}
    )
    assert first != _q2_batch_model_run_id(  # type: ignore[arg-type]
        **{**identity, "pipeline_generation": 2}
    )
    assert first != _q2_batch_model_run_id(  # type: ignore[arg-type]
        **{**identity, "production_run_id": uuid4()}
    )
    for name in (
        "IOC_RULES_BATCH_PROMPT_VERSION",
        "Q2_MARKDOWN_PARSER_VERSION",
        "Q2_BATCH_PARSER_VERSION",
        "Q2_ROUTING_POLICY_VERSION",
    ):
        with monkeypatch.context() as patched:
            patched.setattr(production_workflow, name, "next")
            assert first != _q2_batch_model_run_id(**identity), name  # type: ignore[arg-type]


def test_batch_identity_ignores_q1_parser_and_individual_ioc_rules_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "production_run_id": uuid4(),
        "pipeline_generation": 1,
        "canonical_urls": ("https://example.test/a", "https://example.test/b"),
    }
    first = _q2_batch_model_run_id(**identity)  # type: ignore[arg-type]
    monkeypatch.setattr(production_workflow, "PARSER_VERSION", "q1-next")
    monkeypatch.setattr(production_parsers, "PARSER_VERSION", "q1-next")
    monkeypatch.setattr(production_prompts, "IOC_RULES_PROMPT_VERSION", "999")
    assert _q2_batch_model_run_id(**identity) == first  # type: ignore[arg-type]


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
) -> tuple[object, SubjectProductionRun, _CacheState, _ExtractionSink, _BatchGateway]:
    subject = uuid4()
    blobs = _ArchivedBlobs()
    documents: dict[UUID, SimpleNamespace] = {}
    collections: dict[UUID, SimpleNamespace] = {}
    for index in range(1, count + 1):
        source = _source(index)
        document = _archived_document(
            subject_id=subject,
            url=source.canonical_url,
            content=(f"ARCHIVED {source.canonical_url} " * 50).encode(),
            blobs=blobs,
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
async def test_four_light_sources_use_one_web_batch_of_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _batch_response(
        *(f"IOC confirmed domain\n- domain-{index}.security-lab.io" for index in range(1, 5))
    )
    orchestrator, run, state, sink, gateway = _batch_workflow(monkeypatch, 4, response)

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert len(gateway.calls) == 1
    request = gateway.calls[0]
    assert request.prompt_template_id == "production-q2-ioc-batch"
    assert request.web_search is True
    assert request.routing_hint is ModelRoutingHint.WEB_RESEARCH
    for index in range(1, 5):
        assert f"B{index} https://example.test/source-{index}" in request.text
    assert "ARCHIVED" not in request.text
    assert "@@Q2IN" not in request.text
    assert result["model_calls"] == 1
    assert result["light_calls"] == 1
    assert result["light_batches"] == 1
    assert result["light_sources_batched"] == 4
    # The batch never reads or writes a content-addressed checkpoint.
    assert state.extractions.rows == {}
    assert state.extractions.lookups == 0
    assert run.extraction_progress["model_calls"] == 1  # type: ignore[index]
    assert len(sink.calls[-1]["canonical_json"]["items"]) == 0  # type: ignore[index]


@pytest.mark.asyncio
async def test_batch_progress_marks_only_current_sources_running_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _batch_response(
        *(f"IOC confirmed domain\n- domain-{index}.security-lab.io" for index in range(1, 5))
    )
    orchestrator, run, _state, _sink, gateway = _batch_workflow(monkeypatch, 4, response)
    snapshots: list[dict[str, object]] = []

    async def persist(run_id: UUID, progress: dict[str, object]) -> None:
        del run_id
        snapshots.append(deepcopy(progress))
        run.extraction_progress = deepcopy(progress)

    orchestrator._persist_extraction_progress = persist  # type: ignore[method-assign]

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success"
    assert len(gateway.calls) == 1
    assert all(item["status"] == "pending" for item in snapshots[0]["sources"])
    assert snapshots[0]["active_source_id"] is None
    before_call = snapshots[1]
    assert [item["status"] for item in before_call["sources"]] == [
        "running",
        "running",
        "running",
        "running",
    ]
    assert before_call["active_source_id"] in {f"S{index}" for index in range(1, 5)}


@pytest.mark.asyncio
async def test_later_batch_remains_pending_until_its_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_response = _batch_response(
        *(f"IOC confirmed domain\n- domain-{index}.security-lab.io" for index in range(1, 5))
    )
    second_response = _batch_response(
        *(f"IOC confirmed domain\n- domain-{index}.security-lab.io" for index in range(5, 9))
    )
    third_response = _batch_response(
        *(f"IOC confirmed domain\n- domain-{index}.security-lab.io" for index in range(9, 11))
    )
    orchestrator, run, _state, _sink, gateway = _batch_workflow(monkeypatch, 10, first_response)
    gateway.responses.append(second_response)
    gateway.responses.append(third_response)
    snapshots: list[dict[str, object]] = []

    async def persist(run_id: UUID, progress: dict[str, object]) -> None:
        del run_id
        snapshots.append(deepcopy(progress))
        run.extraction_progress = deepcopy(progress)

    orchestrator._persist_extraction_progress = persist  # type: ignore[method-assign]

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success"
    assert len(gateway.calls) == 3
    second_call = next(
        snapshot
        for snapshot in snapshots
        if {item["source_id"]: item["status"] for item in snapshot["sources"]}["S5"] == "running"
    )
    statuses = {item["source_id"]: item["status"] for item in second_call["sources"]}
    assert all(statuses[f"S{index}"] == "succeeded" for index in range(1, 5))
    assert all(statuses[f"S{index}"] == "running" for index in range(5, 9))


@pytest.mark.asyncio
async def test_batch_source_unavailable_uses_archive_fallback_for_only_that_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _batch_response(
        "IOC confirmed domain\n- domain-1.security-lab.io",
        "UNAVAILABLE",
        "IOC confirmed domain\n- domain-3.security-lab.io",
    )
    gateway = _BatchGateway([response, "EMPTY"])
    orchestrator, run, state, sink, gateway = _batch_workflow(
        monkeypatch, 3, response, gateway=gateway
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert result["completed_source_ids"] == ["S1", "S3", "S2"]
    assert result["skipped_source_ids"] == []
    assert len(gateway.calls) == 2
    assert gateway.calls[1].web_search is False
    assert gateway.calls[1].metadata["source_id"] == "S2"
    assert state.extractions.rows == {}
    assert sink.calls  # The archive fallback makes extraction non-blocking.


@pytest.mark.asyncio
async def test_malformed_sibling_block_fails_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = "\n".join(
        (
            _block("B1", "IOC confirmed domain\n- domain-1.security-lab.io"),
            _block("B2", "RULE yara: broken\n```yara\nrule broken {"),
            _block("B3", "IOC confirmed domain\n- domain-3.security-lab.io"),
        )
    )
    orchestrator, run, _state, _sink, gateway = _batch_workflow(monkeypatch, 3, response)

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "needs_review"
    assert result["completed_source_ids"] == ["S1", "S3"]
    assert result["source_failures"]["S2"]["error_code"] == "batch_source_invalid"
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_unreadable_batch_response_is_a_global_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, _state, sink, gateway = _batch_workflow(
        monkeypatch, 3, "no marker at all, just prose"
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "needs_review"
    assert result["error_code"] == "batch_response_failure"
    assert len(gateway.calls) == 1
    assert sink.calls == []


@pytest.mark.asyncio
async def test_single_light_candidate_uses_the_individual_path(
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
    assert state.extractions.rows == {}


@pytest.mark.asyncio
async def test_batch_ioc_absent_from_the_local_archive_is_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archived text of a batched source never gates its web reading."""
    response = _batch_response(
        "IOC confirmed domain\n- visual-1.security-lab.io",
        "IOC confirmed domain\n- visual-2.security-lab.io",
    )
    orchestrator, run, _state, sink, gateway = _batch_workflow(monkeypatch, 2, response)

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert len(gateway.calls) == 1
    canonical = sink.calls[-1]["canonical_json"]  # type: ignore[index]
    assert canonical["items"] == []


@pytest.mark.asyncio
async def test_batch_ioc_rules_drops_fact_from_one_source_without_affecting_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _batch_response(
        "FACT actors\n- Should not survive\nIOC confirmed domain\n- evil.security-lab.io",
        "IOC confirmed domain\n- sibling.security-lab.io",
    )
    orchestrator, run, _state, sink, gateway = _batch_workflow(monkeypatch, 2, response)

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", date(2026, 7, 10)),)),
    )

    assert result["status"] == "success", result
    assert len(gateway.calls) == 1
    canonical = sink.calls[-1]["canonical_json"]
    assert canonical["items"] == []
    assert all(item["category"] != "actors" for item in canonical["items"])
    warnings = sink.calls[-1]["warnings"]
    assert isinstance(warnings, list)
    assert warnings.count("q2_ioc_rules_fact_dropped") == 1


@pytest.mark.asyncio
async def test_full_sources_are_never_batched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, state, sink, gateway = _batch_workflow(
        monkeypatch,
        1,
        "FACT malware\n- ExampleRAT",
        gateway=_BatchGateway(["FACT malware\n- ExampleRAT"]),
    )
    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/source-1", date(2026, 7, 10)),)),
    )
    assert result["status"] == "success"
    assert result["light_batches"] == 0
    assert gateway.calls[0].prompt_template_id == "production-q2-url"
    assert state.extractions.rows == {}
    canonical = sink.calls[-1]["canonical_json"]
    assert [item["value"] for item in canonical["items"]] == ["ExampleRAT"]


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
async def test_retry_of_the_same_run_reuses_the_batch_model_run(
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
    retry = await orchestrator._execute_direct_url_extraction(run, snapshot=snapshot)

    assert first["status"] == "success", first
    assert retry["status"] == "success", retry
    assert len(adapter.calls) == 1
    assert len(model_uow.state) == 1
    assert retry["model_calls"] == 0

    # A new production run is a new reading of mutable web sources.
    replay = SubjectProductionRun(
        subject_id=run.subject_id,
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.EXTRACTION,
    )
    state._runs[replay.id] = replay
    second = await orchestrator._execute_direct_url_extraction(replay, snapshot=snapshot)

    assert second["status"] == "success"
    assert len(adapter.calls) == 2
    assert len(model_uow.state) == 2
    assert state.extractions.rows == {}

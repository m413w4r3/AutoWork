from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_workflow
from cti_app.application.production_artifact_verification import (
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2SourceOutput,
    ReferenceReport,
)
from cti_app.application.production_q2_batch import q2_batch_output_marker
from cti_app.domain.production import SubjectProductionRun, SubjectProductionStage
from tests.test_production_extraction_profiles import (
    _archived_document,
    _cached_orchestrator,
    _CacheState,
    _CacheStore,
    _collection_for,
    _ExtractionSink,
    _input_source,
    _snapshot,
)
from tests.test_production_q2_batch import _source


class _ArchiveReader:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}

    async def read_blob(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self.contents[blob_id]
        if len(content) > max_bytes:
            raise ValueError("archive is too large")
        return content


class _Gateway:
    def __init__(self, response: str | list[str]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.requests: list[object] = []

    async def execute(self, request: object, role: object) -> object:
        del role
        self.requests.append(request)
        return SimpleNamespace(
            output_text=self.responses.pop(0),
            run=SimpleNamespace(
                id=request.run_id,
                status=production_workflow.ModelRunStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                error_details=None,
            ),
            metadata={},
        )


class _RecordingDiagnostics:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record(self, **fields: object) -> None:
        self.events.append(fields)

    def record_parse(self, **fields: object) -> None:
        del fields


def _block(number: int, body: str) -> str:
    return f"{q2_batch_output_marker(f'B{number}')}\n{body}"


def _batch_response(*bodies: str) -> str:
    return "\n\n".join(_block(index, body) for index, body in enumerate(bodies, 1))


def _workflow(
    monkeypatch: pytest.MonkeyPatch,
    archives: list[bytes],
    response: str | list[str],
) -> tuple[object, SubjectProductionRun, _CacheState, _ExtractionSink, _ArchiveReader]:
    subject = uuid4()
    documents: dict[UUID, SimpleNamespace] = {}
    collections: dict[UUID, SimpleNamespace] = {}
    reader = _ArchiveReader()
    for index, content in enumerate(archives, 1):
        source = _source(index)
        document = _archived_document(
            subject_id=subject,
            url=source.canonical_url,
            content=content,
        )
        documents[document.id] = document
        reader.contents[document.decoded_blob_id] = content
        collections[uuid4()] = _collection_for(document, source.canonical_url)

    state = _CacheState(documents, collections)
    sink = _ExtractionSink()
    gateway = _Gateway(response)
    orchestrator = _cached_orchestrator(
        state,
        gateway,  # type: ignore[arg-type]
        _CacheStore(),
        sink,
        monkeypatch,
    )
    orchestrator._diagnostics = _RecordingDiagnostics()
    orchestrator._blob_reader = reader
    report = ReferenceReport(
        sources=tuple(_source(index) for index in range(1, len(archives) + 1)),
        events=(),
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
    return orchestrator, run, state, sink, reader


@pytest.mark.asyncio
async def test_batch_gate_uses_the_archive_of_the_framed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [b"b1.security-lab.io", b"b2.security-lab.io"],
        _batch_response(
            "IOC confirmed domain\n- b2.security-lab.io",
            "IOC confirmed domain\n- b2.security-lab.io",
        ),
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "success", result
    items = sink.calls[-1]["canonical_json"]["items"]
    assert [item["value"] for item in items] == ["b2.security-lab.io"]
    assert items[0]["source_ids"] == ["S2"]
    assert any(
        "q2_batch_source_evidence_rejected:B1:S1:domain:count=1:reason=source_evidence_missing"
        in warning
        for warning in sink.calls[-1]["warnings"]
    )
    verification_diagnostics = cast(
        dict[str, object], sink.calls[-1]["verification_diagnostics"]
    )
    groups = verification_diagnostics["q2_source_evidence_rejection_groups"]
    assert groups == [
        {
            "source_id": "S1",
            "batch_id": "B1",
            "artifact_type": "domain",
            "rejection_count": 1,
            "reason_code": "source_evidence_missing",
        }
    ]
    rejections = cast(
        list[dict[str, object]], verification_diagnostics["q2_source_evidence_rejections"]
    )
    assert rejections == [
        {
            "source_id": "S1",
            "source_url": "https://example.test/source-1",
            "batch_id": "B1",
            "model_run_id": rejections[0]["model_run_id"],
            "proposal_index": 1,
            "proposal_kind": "artifact",
            "artifact_type": "domain",
            "reason_code": "source_evidence_missing",
            "value": "b2.security-lab.io",
            "value_hash": hashlib.sha256(b"b2.security-lab.io").hexdigest(),
        }
    ]
    assert verification_diagnostics["q2_rejected_rules"] == []
    assert verification_diagnostics["q2_rejected_rule_count"] == 0
    assert verification_diagnostics["q2_rejected_artifact_count"] == 1
    assert not any(
        warning.startswith("q2_detection_rules_lost:")
        for warning in sink.calls[-1]["warnings"]
    )
    evidence_events = [
        event
        for event in orchestrator._diagnostics.events  # type: ignore[attr-defined]
        if event.get("event") == "q2.source.evidence_rejected"
    ]
    assert evidence_events[0]["value"] == "b2.security-lab.io"


@pytest.mark.asyncio
async def test_shared_ioc_keeps_both_real_source_provenances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [b"shared.security-lab.io", b"shared.security-lab.io"],
        _batch_response(
            "IOC confirmed domain\n- shared.security-lab.io",
            "IOC confirmed domain\n- shared.security-lab.io",
        ),
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "success", result
    item = sink.calls[-1]["canonical_json"]["items"][0]
    assert item["source_ids"] == ["S1", "S2"]


@pytest.mark.asyncio
async def test_batch_gate_records_rejected_artifact_and_rule_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected_artifact = "foreign.security-lab.io"
    rejected_rule = "rule Lost { condition: true }"
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [b"present.security-lab.io", b"b2.security-lab.io"],
        _batch_response(
            "IOC confirmed domain\n"
            f"- {rejected_artifact}\n"
            "RULE yara: Lost\n"
            f"```yara\n{rejected_rule}\n```",
            "IOC confirmed domain\n- b2.security-lab.io",
        ),
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "success", result
    verification_diagnostics = cast(
        dict[str, object], sink.calls[-1]["verification_diagnostics"]
    )
    rejections = cast(
        list[dict[str, object]], verification_diagnostics["q2_source_evidence_rejections"]
    )
    assert {rejection["value"] for rejection in rejections} == {
        rejected_artifact,
        rejected_rule,
    }
    assert verification_diagnostics["q2_rejected_rule_count"] == 1
    assert verification_diagnostics["q2_rejected_artifact_count"] == 1
    rejected_rules = cast(
        list[dict[str, object]], verification_diagnostics["q2_rejected_rules"]
    )
    assert [rejection["value"] for rejection in rejected_rules] == [rejected_rule]
    warnings = cast(list[str], sink.calls[-1]["warnings"])
    assert "q2_detection_rules_lost:count=1" in warnings


@pytest.mark.asyncio
async def test_batch_gate_keeps_valid_ioc_when_a_sibling_is_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [b"valid.security-lab.io"],
        "IOC confirmed domain\n- valid.security-lab.io\n"
        "IOC confirmed domain\n- foreign.security-lab.io",
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "success", result
    assert [item["value"] for item in sink.calls[-1]["canonical_json"]["items"]] == [
        "valid.security-lab.io"
    ]
    assert run.extraction_progress["confirmed_iocs"] == 1
    assert run.extraction_progress["sources"][0]["ioc_count"] == 1
    assert sink.calls[-1]["canonical_json"]["rules"] == []


@pytest.mark.asyncio
async def test_batch_gate_applies_literal_rule_matching_per_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_one = "rule One { condition: true }"
    body_two = "rule Two { condition: true }"
    response = _batch_response(
        f"RULE yara: One\n```yara\n{body_two}\n```",
        f"RULE yara: Two\n```yara\n{body_two}\n```",
    )
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [body_one.encode(), body_two.encode()],
        response,
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "success", result
    rules = sink.calls[-1]["canonical_json"]["rules"]
    assert [rule["body"] for rule in rules] == [body_two]
    assert rules[0]["source_ids"] == ["S2"]
    verification_diagnostics = cast(
        dict[str, object], sink.calls[-1]["verification_diagnostics"]
    )
    rejected_rules = cast(
        list[dict[str, object]], verification_diagnostics["q2_rejected_rules"]
    )
    assert rejected_rules == [
        {
            "source_id": "S1",
            "source_url": "https://example.test/source-1",
            "batch_id": "B1",
            "model_run_id": rejected_rules[0]["model_run_id"],
            "proposal_index": 1,
            "proposal_kind": "rule",
            "artifact_type": "yara",
            "reason_code": "source_rule_evidence_missing",
            "value": body_two,
            "value_hash": hashlib.sha256(body_two.encode()).hexdigest(),
        }
    ]
    assert verification_diagnostics["q2_rejected_rule_count"] == 1
    assert verification_diagnostics["q2_rejected_artifact_count"] == 0
    assert any(
        warning == "q2_detection_rules_lost:count=1"
        for warning in sink.calls[-1]["warnings"]
    )


@pytest.mark.asyncio
async def test_full_gate_preserves_facts_but_filters_structured_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [b"confirmed.security-lab.io"],
        "FACT malware\n- ExampleRAT\nIOC confirmed domain\n- confirmed.security-lab.io\n"
        "IOC confirmed domain\n- foreign.security-lab.io",
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source(_source(1).canonical_url, None),)),
    )

    assert result["status"] == "success", result
    items = sink.calls[-1]["canonical_json"]["items"]
    assert [item["value"] for item in items] == [
        "ExampleRAT",
        "confirmed.security-lab.io",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["missing", "sha"])
async def test_archive_unavailable_or_tampered_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    orchestrator, run, state, sink, reader = _workflow(
        monkeypatch,
        [b"valid.security-lab.io"],
        "IOC confirmed domain\n- valid.security-lab.io",
    )
    document = next(iter(state._docs_by_id.values()))
    if tamper == "missing":
        del reader.contents[document.decoded_blob_id]
    else:
        document.decoded_sha256 = hashlib.sha256(b"different").hexdigest()

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "needs_review", result
    assert result["source_failures"]["S1"]["error_code"] == ("q2_source_evidence_unavailable")
    assert sink.calls == []
    assert run.extraction_progress["sources"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_live_unavailable_uses_one_archive_fallback_without_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = b"archive-ioc.security-lab.io " * 50
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [archive],
        ["UNAVAILABLE", "IOC confirmed domain\n- archive-ioc.security-lab.io"],
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "success", result
    assert result["completed_source_ids"] == ["S1"]
    assert result["skipped_source_ids"] == []
    assert result["failed_source_ids"] == []
    assert len(orchestrator._model_gateway.requests) == 2  # type: ignore[attr-defined]
    live_request, archive_request = orchestrator._model_gateway.requests  # type: ignore[attr-defined]
    assert live_request.web_search is True
    assert archive_request.web_search is False
    assert "archive-ioc.security-lab.io" in archive_request.text
    assert "Canonical source URL (provenance only):" in archive_request.text
    assert "Do not browse the web." in archive_request.text
    assert "Open this exact source:" not in archive_request.text
    assert sink.calls[-1]["verification_diagnostics"]["source_skips"] == {}
    events = orchestrator._diagnostics.events  # type: ignore[attr-defined]
    fallback_events = [
        event for event in events if event.get("event") == "q2.source.archive_fallback_completed"
    ]
    assert fallback_events[0]["source_content_sha256"] == hashlib.sha256(archive).hexdigest()
    assert fallback_events[0]["live_failure_code"] == "q2_source_unavailable"


@pytest.mark.asyncio
async def test_live_unavailable_without_usable_archive_is_a_successful_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [b""],
        "UNAVAILABLE",
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "success", result
    assert result["completed_source_ids"] == []
    assert result["skipped_source_ids"] == ["S1"]
    assert result["failed_source_ids"] == []
    assert len(orchestrator._model_gateway.requests) == 1  # type: ignore[attr-defined]
    diagnostics = sink.calls[-1]["verification_diagnostics"]
    assert diagnostics["source_skips"]["S1"]["blocking"] is False
    assert "q2_source_coverage_failed" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_state", ["missing_blob", "unreadable", "sha_mismatch"])
async def test_live_unavailable_archive_integrity_failures_are_non_blocking_skips(
    monkeypatch: pytest.MonkeyPatch,
    archive_state: str,
) -> None:
    orchestrator, run, state, sink, reader = _workflow(
        monkeypatch,
        [b"archive-ioc.security-lab.io"],
        "UNAVAILABLE",
    )
    document = next(iter(state._docs_by_id.values()))
    collection = next(iter(state._collections_by_id.values()))
    if archive_state == "missing_blob":
        document.decoded_blob_id = None
        collection.decoded_blob_id = None
    elif archive_state == "unreadable":
        reader.contents.clear()
    else:
        document.decoded_sha256 = "f" * 64

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "success", result
    assert result["skipped_source_ids"] == ["S1"]
    assert result["failed_source_ids"] == []
    assert len(orchestrator._model_gateway.requests) == 1  # type: ignore[attr-defined]
    assert sink.calls[-1]["verification_diagnostics"]["source_skips"]["S1"]["blocking"] is False


@pytest.mark.asyncio
async def test_archive_fallback_invalid_output_remains_a_real_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, run, _state, sink, _reader = _workflow(
        monkeypatch,
        [b"archive-ioc.security-lab.io " * 50],
        ["UNAVAILABLE", "not Q2 markdown"],
    )

    result = await orchestrator._execute_direct_url_extraction(
        run,
        snapshot=_snapshot((_input_source("https://example.test/core", None),)),
    )

    assert result["status"] == "needs_review", result
    assert result["error_code"] == "q2_source_coverage_failed"
    assert result["failed_source_ids"] == ["S1"]
    assert result["skipped_source_ids"] == []
    assert sink.calls == []


def test_hatching_article_cannot_borrow_triage_iocs() -> None:
    triage_hash = "a" * 64
    hatching = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="evil.example",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
            Q2ArtifactProposal(
                value=triage_hash,
                artifact_type="hash",
                indicator_status="confirmed_ioc",
            ),
        ]
    )
    triage = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="evil.example",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
            Q2ArtifactProposal(
                value=triage_hash,
                artifact_type="hash",
                indicator_status="confirmed_ioc",
            ),
        ]
    )

    hatching_gate = production_workflow.verify_q2_output_against_source(
        hatching,
        "NightLedger detection added; sample report linked.",
    )
    triage_gate = production_workflow.verify_q2_output_against_source(
        triage,
        f"Triage sample report\nevil.example\nSHA256 {triage_hash}",
    )

    assert hatching_gate.output.artifacts == []
    assert all(
        rejection.reason_code == "source_evidence_missing"
        for rejection in hatching_gate.rejections
    )
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(output=triage_gate.output, source_ids=("S8",)),
        )
    )
    assert {item.source_ids for item in verification.canonical.items} == {("S8",)}

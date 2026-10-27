from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisRequest,
    ConfinedAnalysisResult,
    ConfinedAnalysisTool,
    ConfinedAnalysisWorkerAttestation,
)
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_acquisition_tools import (
    LOCAL_ANALYSIS_CAPABILITY,
    LOCAL_QUALIFICATION_CAPABILITY,
    VT_FILE_DOWNLOAD_CAPABILITY,
    LocalSampleAnalysisService,
    register_acquisition_tools,
)
from cti_app.application.investigation_governance import (
    InvestigationEnvelopeService,
    SampleQualificationService,
)
from cti_app.application.investigation_persistence import InMemorySampleAssociationRepository
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolEffect,
    ToolExecutionContext,
    ToolResult,
    validate_json_schema,
)
from cti_app.application.virustotal import VirusTotalHttpError
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetReservationStatus,
    EvidenceKind,
    SampleOrigin,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import StopReason
from tests.investigation_c2_fakes import (
    FakeDatabase,
    FakeUow,
    FakeVirusTotalPort,
    make_approved_proposal,
    make_evidence_recorder,
    make_investigation,
    make_sample_acquisition_service,
)
from tests.test_investigation_service import _decision, _Fixture


class _AttestedAnalysisWorker:
    def __init__(self, worker_result: dict[str, Any]) -> None:
        self.available = True
        self.available_reason = None
        self.isolation_profile = "analysis-worker-v1"
        self.family_statuses = {
            "sample_analysis": ConfinedAnalysisFamilyStatus(
                "sample_analysis", True, "static+capa+smda"
            )
        }
        self.attestation = ConfinedAnalysisWorkerAttestation(
            reachable=True,
            confined=True,
            isolation_profile=self.isolation_profile,
            versions={"static": "static-v1", "capa": "9.4.0", "smda": "smda-v1"},
            families=self.family_statuses,
        )
        self.worker_result = worker_result
        self.requests: list[ConfinedAnalysisRequest] = []

    async def execute(self, request: ConfinedAnalysisRequest) -> ConfinedAnalysisResult:
        self.requests.append(request)
        encoded = json.dumps(
            self.worker_result, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        operation_digest = hashlib.sha256(
            f"{request.sha256}:{request.tool.value}".encode()
        ).hexdigest()
        return ConfinedAnalysisResult(
            ToolInvocationStatus.SUCCEEDED,
            f"confined-analysis:{operation_digest}",
            request.artifact_id,
            request.sha256,
            result_blob_id=uuid4(),
            result_sha256=hashlib.sha256(encoded).hexdigest(),
            payload={"result": self.worker_result},
        )


def _worker_sample_result(*, status: str, string_value: str) -> dict[str, Any]:
    return {
        "status": status,
        "families": {
            "static": {
                "status": "SUCCEEDED",
                "version": "static-v1",
                "strings_observed": 1,
                "strings": [{"value": string_value, "encoding": "ascii", "offset": 4}],
            },
            "capa": {"status": "SUCCEEDED", "version": "9.4.0", "capabilities": []},
            "smda": {
                "status": "SUCCEEDED",
                "version": "smda-v1",
                "architecture": "x86",
                "code_features": {"packing": {"packed": False}, "ngrams": []},
            },
            "floss": {"status": "SUCCEEDED", "version": "3.1.1", "strings": []},
        },
    }


def _register(database: FakeDatabase, hits: HitAcquisitionService):
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
    )
    return registry


class _RouteRefusedPort(FakeVirusTotalPort):
    async def file_download(self, file_hash: str, *, sink) -> None:
        self.download_calls.append(file_hash)
        raise VirusTotalHttpError(
            "synthetic route absent",
            code="virustotal_not_found",
            status_code=404,
        )


class _PartialDownloadTimeoutPort(FakeVirusTotalPort):
    async def file_download(self, file_hash: str, *, sink) -> None:
        self.download_calls.append(file_hash)
        sink.write(self.sample_bytes[:8])
        raise TimeoutError("synthetic partial download timeout")


class _NoExistingSamples:
    async def list_by_subject_and_sha256(self, subject_id: UUID, sha256: str) -> tuple[Any, ...]:
        del subject_id, sha256
        return ()


@pytest.mark.asyncio
async def test_tool_capabilities_and_unavailable_analysis_are_explicit() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Approve bounded VT access.",
        correlation_id="g2:tools",
    )
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=None,
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    download_definition, _ = registry.get("vt.download_sample", "1")
    analyze_definition, _ = registry.get("sample.analyze", "1")
    qualification_definition, _ = registry.get("sample.set_qualification", "1")
    assert download_definition.capability_key == VT_FILE_DOWNLOAD_CAPABILITY
    assert download_definition.effect is ToolEffect.ACQUIRE
    assert analyze_definition.capability_key == LOCAL_ANALYSIS_CAPABILITY
    assert qualification_definition.capability_key == LOCAL_QUALIFICATION_CAPABILITY
    validate_json_schema(
        {
            "association_id": str(UUID(int=1)),
            "qualification": "uncertain",
            "reason": "Keep the relationship tentative.",
            "evidence_refs": [],
        },
        qualification_definition.input_schema,
    )
    with pytest.raises(ValueError):
        validate_json_schema(
            {
                "association_id": str(UUID(int=1)),
                "qualification": "confirmed",
                "reason": "The agent cannot make G3.",
                "evidence_refs": [],
            },
            qualification_definition.input_schema,
        )

    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:analyze:unavailable",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )
    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(UUID(int=2)),
            "purpose": "Analyze quarantined sample",
            "evidence_refs": [],
        },
        purpose="Analyze quarantined sample",
        operation_key="op:sample:analyze:unavailable",
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    missing_capability_context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:analyze:missing-capability",
        available_capabilities=frozenset(),
    )
    missing_capability_result = await RegistryToolDispatcher(registry).dispatch(
        missing_capability_context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(UUID(int=2)),
            "purpose": "Capability absence check",
            "evidence_refs": [],
        },
        purpose="Capability absence check",
        operation_key="op:sample:analyze:missing-capability",
    )
    assert missing_capability_result.status is ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_invalid_download_arguments_are_definitive_local_failures() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope = await InvestigationEnvelopeService(lambda: FakeUow(database)).approve(
        make_approved_proposal(investigation),
        actor_id="analyst:synthetic",
        reason="Approve bounded synthetic VT access.",
        correlation_id="g2:invalid-download-arguments",
    )
    port = FakeVirusTotalPort(b"synthetic payload")
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    dispatcher = RegistryToolDispatcher(registry)
    valid_hash = hashlib.sha256(b"synthetic payload").hexdigest()
    cases = (
        {"sha256": valid_hash, "purpose": ["first", "second"], "evidence_refs": []},
        {"sha256": valid_hash, "purpose": "Review this sample."},
        {
            "sha256": valid_hash,
            "purpose": "Review this sample.",
            "evidence_refs": [],
            "unexpected": True,
        },
    )

    for index, arguments in enumerate(cases):
        result = await dispatcher.dispatch(
            ToolExecutionContext(
                investigation_id=investigation.id,
                envelope=envelope,
                budget=envelope.budget,
                idempotency_key=f"invalid-download-{index}",
                available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
            ),
            tool_name="vt.download_sample",
            schema_version="1",
            arguments=arguments,
            purpose="Validate the real acquisition schema.",
            operation_key=f"invalid-download-{index}",
        )
        assert result.status is ToolInvocationStatus.FAILED
        assert result.error_code == "invalid_tool_arguments"
        assert result.output["validation_errors"]

    assert port.download_calls == []


@pytest.mark.asyncio
async def test_invalid_download_retry_is_bounded() -> None:
    database = FakeDatabase()
    fixture = _Fixture(
        responses=[
            _decision(
                goal="Validate sample arguments.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": {
                            "sha256": "a" * 64,
                            "purpose": ["first phrase", "second phrase"],
                            "evidence_refs": [],
                        },
                        "purpose": "Request the synthetic sample.",
                    },
                ),
            ),
            _decision(
                goal="Retry the same invalid sample arguments.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": {
                            "sha256": "a" * 64,
                            "purpose": ["first phrase", "second phrase"],
                            "evidence_refs": [],
                        },
                        "purpose": "Request the synthetic sample.",
                    },
                ),
            ),
            _decision(
                goal="Repeat the same invalid request after two local rejections.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": {
                            "sha256": "a" * 64,
                            "purpose": ["first phrase", "second phrase"],
                            "evidence_refs": [],
                        },
                        "purpose": "Request the synthetic sample.",
                    },
                ),
            ),
            _decision(
                goal="Finish after the repeated rejection is reported.",
                next_action="finish",
            ),
        ],
        tools=(
            (
                "vt.download_sample",
                "vt",
                ToolEffect.ACQUIRE,
                VT_FILE_DOWNLOAD_CAPABILITY,
                {BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 1},
            ),
        ),
        no_progress_limit=6,
    )
    fixture.envelope = replace(fixture.envelope, acquire_hits_allowed=True)
    database.investigations[fixture.investigation_id] = make_investigation()
    port = FakeVirusTotalPort(b"synthetic sample")
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    actual_registry = _register(database, hits)
    definition, adapter = actual_registry.get("vt.download_sample", "1")
    fixture.registry.unregister("vt.download_sample", "1")
    fixture.registry.register(definition, adapter)

    initial = await fixture.start()
    state = await fixture.service.run(initial.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(fixture.investigation_id)
    assert state.stop_reason is StopReason.AGENT_FINISHED
    assert len(invocations) == 2, [
        (item.operation_key, item.normalized_parameters, item.status, item.response_refs)
        for item in invocations
    ]
    assert all(item.status is ToolInvocationStatus.FAILED for item in invocations)
    assert all(item.response_refs == () for item in invocations)
    assert all(item.consumed_budget == {} for item in invocations)
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    assert envelope is not None
    reservations = {item.operation_key: item for item in envelope.budget.reservations}
    assert all(
        reservations[item.operation_key].status is BudgetReservationStatus.RELEASED
        for item in invocations
    )
    assert port.download_calls == []
    assert len(fixture.bridge_transport.prompts) == 4
    assert "invalid_tool_arguments" in fixture.bridge_transport.prompts[1]
    assert "purpose" in fixture.bridge_transport.prompts[1]
    assert "expected string" in fixture.bridge_transport.prompts[1]
    assert "failed definitively twice" in fixture.bridge_transport.prompts[3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported_status",
    (ToolInvocationStatus.NEEDS_REVIEW, ToolInvocationStatus.FAILED),
)
async def test_ambiguous_download_failure_stops_without_persisting_adapter_output(
    reported_status: ToolInvocationStatus,
) -> None:
    database = FakeDatabase()
    sample = b"synthetic acquisition timeout sample"
    sha256 = hashlib.sha256(sample).hexdigest()
    arguments = {"sha256": sha256, "purpose": "Request the synthetic sample.", "evidence_refs": []}
    fixture = _Fixture(
        responses=[
            _decision(
                goal="Request the synthetic sample.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": arguments,
                        "purpose": "Request the synthetic sample.",
                    },
                ),
            ),
            _decision(
                goal="Retry the same sample download.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": arguments,
                        "purpose": "Retry the synthetic sample.",
                    },
                ),
            ),
        ],
        tools=(
            (
                "vt.download_sample",
                "vt",
                ToolEffect.ACQUIRE,
                VT_FILE_DOWNLOAD_CAPABILITY,
                {BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 1},
            ),
        ),
    )
    fixture.envelope = replace(fixture.envelope, acquire_hits_allowed=True)
    source_associations = InMemorySampleAssociationRepository()
    fixture.service._sample_associations = source_associations
    fixture.service._samples = cast(Any, _NoExistingSamples())
    database.investigations[fixture.investigation_id] = make_investigation()
    port = _PartialDownloadTimeoutPort(sample)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(fixture.investigation_id, sha256, SampleOrigin.VT_PIVOT)
    await source_associations.append_version(association, expected_version=0)
    actual_registry = _register(database, hits)
    definition, adapter = actual_registry.get("vt.download_sample", "1")
    fixture.registry.unregister("vt.download_sample", "1")

    class _PersistedEnvelopeAdapter:
        async def execute(
            self, call_arguments: Mapping[str, Any], context: ToolExecutionContext
        ) -> ToolResult:
            # Production repositories share this versioned envelope snapshot.
            database.envelopes[context.investigation_id] = context.envelope
            result = cast(ToolResult, await adapter.execute(call_arguments, context))
            if reported_status is ToolInvocationStatus.FAILED:
                return replace(
                    result,
                    status=reported_status,
                    output={**result.output, "adapter_detail": "partial response is ambiguous"},
                )
            return result

    fixture.registry.register(definition, _PersistedEnvelopeAdapter())

    initial = await fixture.start()
    state = await fixture.service.run(initial.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(fixture.investigation_id)
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    assert state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.NEEDS_REVIEW
    # The framework records only a fixed diagnostic; the adapter's partial
    # output is never promoted into evidence.
    refs = invocations[0].response_refs
    assert len(refs) == 1
    diagnostics = await fixture.observations.list_for_investigation(fixture.investigation_id)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == refs[0]
    assert diagnostics[0].object_type in {"tool_call", "tool_result"}
    assert diagnostics[0].source == "vt.download_sample"
    assert invocations[0].consumed_budget == {}
    assert envelope is not None
    reservation = next(
        item
        for item in envelope.budget.reservations
        if item.operation_key == invocations[0].operation_key
    )
    assert reservation.status is BudgetReservationStatus.RESERVED
    assert reservation.committed_amounts == {}
    assert envelope.budget.reserved[BudgetDimension.VT_UNITS] == 1
    assert envelope.budget.reserved[BudgetDimension.DOWNLOADS] == 1
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
    assert envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 0
    assert port.download_calls == [sha256]
    assert fixture.bridge_transport.prompts
    assert len(fixture.bridge_transport.prompts) == 1


@pytest.mark.asyncio
async def test_download_tool_requests_permission_outside_g2_envelope() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"request-permission sample"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation, allowed_providers=(), acquire_hits_allowed=False),
        actor_id="analyst:alice",
        reason="Local analysis only; defer VT acquisition.",
        correlation_id="g2:local-only",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:download:out-of-envelope",
        available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
    )
    _definition, adapter = registry.get("vt.download_sample", "1")

    adapter_result = await adapter.execute(
        {
            "sha256": hashlib.sha256(content).hexdigest(),
            "purpose": "Request an approved acquisition",
            "evidence_refs": [],
        },
        context,
    )
    dispatched_result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments={
            "sha256": hashlib.sha256(content).hexdigest(),
            "purpose": "Request an approved acquisition",
            "evidence_refs": [],
        },
        purpose="Request an approved acquisition",
        operation_key="op:download:out-of-envelope",
    )

    assert adapter_result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert adapter_result.output["request_permission"] is True
    # The existing dispatcher rejects ACQUIRE before reaching the adapter.
    assert dispatched_result.status is ToolInvocationStatus.POLICY_DENIED
    assert port.download_calls == []


@pytest.mark.asyncio
async def test_download_tool_does_not_cache_a_404_with_unknown_response_origin() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:synthetic",
        reason="Approve bounded synthetic VT acquisition.",
        correlation_id="g2:synthetic-route-refusal",
    )
    content = b"synthetic sample for download route refusal"
    port = _RouteRefusedPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    _definition, adapter = registry.get("vt.download_sample", "1")
    arguments = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "purpose": "Review a synthetic hit",
        "evidence_refs": [],
    }

    first = await adapter.execute(
        arguments,
        ToolExecutionContext(
            investigation_id=investigation.id,
            envelope=envelope,
            budget=envelope.budget,
            idempotency_key="synthetic-download-attempt-1",
            available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
        ),
    )
    second = await adapter.execute(
        arguments,
        ToolExecutionContext(
            investigation_id=investigation.id,
            envelope=envelope,
            budget=envelope.budget,
            idempotency_key="synthetic-download-attempt-2",
            available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
        ),
    )

    assert first.status is ToolInvocationStatus.UNAVAILABLE
    assert first.output["error_code"] == "virustotal_download_404_origin_unknown"
    assert first.output["reason"] == "provider_download_404_origin_unknown"
    assert first.output["confirmed_non_execution"] is True
    assert "synthetic route absent" not in first.message
    assert second.output.get("cached", False) is False
    assert port.download_calls == [arguments["sha256"], arguments["sha256"]]


@pytest.mark.asyncio
async def test_dispatcher_download_reuses_one_g2_reservation_on_replay() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"dispatcher idempotency fixture"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Approve one hash-verified hit download.",
        correlation_id="g2:dispatcher-download",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    dispatcher = RegistryToolDispatcher(registry)
    operation_key = "vt.download_sample:stable-operation-1"
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key=operation_key,
        available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
    )
    arguments = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "purpose": "Review this VT pivot.",
        "evidence_refs": [],
    }

    first = await dispatcher.dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments=arguments,
        purpose="Review this VT pivot.",
        operation_key=operation_key,
    )
    replay = await dispatcher.dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments=arguments,
        purpose="Review this VT pivot.",
        operation_key=operation_key,
    )

    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert replay == first
    assert port.download_calls == [arguments["sha256"]]
    assert len(database.attempts) == 1
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 1
    assert envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 1
    assert len(database.decisions) == 1


@pytest.mark.asyncio
async def test_sample_analyze_records_bounded_binary_observations() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"MZ harmless static analysis fixture strings"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Allow bounded candidate acquisition and local analysis.",
        correlation_id="g2:analysis",
    )
    port = FakeVirusTotalPort(content)
    recorder = make_evidence_recorder(database)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=recorder,
    )
    sha256 = hashlib.sha256(content).hexdigest()
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await hits.acquire_hit(association.id, purpose="Review candidate fixture")
    assert acquired.sample_id is not None
    worker = _AttestedAnalysisWorker(
        _worker_sample_result(status="PARTIAL", string_value="harmless static analysis fixture")
    )

    analyzer = LocalSampleAnalysisService(
        hits,
        lambda: FakeUow(database),
        recorder,
        confined_analysis=worker,  # type: ignore[arg-type]
    )
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
        analyzer=analyzer,
    )
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:sample:analysis",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(association.id),
            "purpose": "Extract local static strings",
            "evidence_refs": [],
        },
        purpose="Extract local static strings",
        operation_key="op:sample:analysis",
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["sample_sha256"] == sha256
    assert result.output["qualification"] == "candidate"
    assert len(worker.requests) == 1
    assert worker.requests[0].tool is ConfinedAnalysisTool.SAMPLE_ANALYSIS
    assert worker.requests[0].sha256 == sha256
    assert result.output["extractors"]["static"]["version"] == "static-v1"
    assert result.output["engines_executed"] == ("capa", "floss", "smda", "static")
    assert result.output["engine_statuses"]["static"] == "SUCCEEDED"
    assert result.output["partial_reason"] == (
        "analysis_worker_status:partial; "
        "goodware_measurement_unavailable:goodware_measurement_service_unavailable"
    )
    assert result.output["strings_recorded"] == 1
    assert result.output["goodware"]["available"] is False
    assert {
        item.kind
        for item in database.observations.values()
        if item.kind in {EvidenceKind.BINARY_STRING, EvidenceKind.BINARY_ANALYSIS}
    } == {
        EvidenceKind.BINARY_STRING,
        EvidenceKind.BINARY_ANALYSIS,
    }
    assert len(result.observation_refs) == 2


@pytest.mark.asyncio
async def test_sample_analyze_injects_code_features_and_goodware_measurement() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"MZ harmless code feature and goodware fixture"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Allow bounded candidate acquisition and local analysis.",
        correlation_id="g2:code-features-goodware",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(
        investigation.id,
        hashlib.sha256(content).hexdigest(),
        SampleOrigin.VT_PIVOT,
    )
    acquired = await hits.acquire_hit(association.id, purpose="Review candidate fixture")
    assert acquired.sample_id is not None

    baseline_id = uuid4()

    class BaselineLookup:
        async def get(self, _investigation_id):
            return baseline_id

    class AnalysisUow(FakeUow):
        async def __aenter__(self):
            await super().__aenter__()
            self.investigation_goodware_baselines = BaselineLookup()
            return self

    class ExistingGoodwareService:
        prepared = None
        features = ()

        async def prepare(self, baseline):
            self.prepared = baseline

        async def lookup_batch(self, baseline, features):
            self.features = tuple(features)
            return {self.features[0]: 4} if self.features else {}

    goodware = ExistingGoodwareService()
    worker = _AttestedAnalysisWorker(
        _worker_sample_result(status="SUCCEEDED", string_value="fixture string")
    )
    analyzer = LocalSampleAnalysisService(
        hits,
        lambda: AnalysisUow(database),
        make_evidence_recorder(database),
        goodware=goodware,
        confined_analysis=worker,  # type: ignore[arg-type]
    )
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
        analyzer=analyzer,
    )
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:sample:code-features-goodware",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(association.id),
            "purpose": "Compare local code features with the bound goodware corpus",
            "evidence_refs": [],
        },
        purpose="Run local feature extraction and goodware comparison.",
        operation_key="op:sample:code-features-goodware",
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["extractors"]["smda"]["version"] == "smda-v1"
    assert result.output["code_features"]["packing"]["packed"] is False
    assert result.output["goodware"]["available"] is True
    assert result.output["goodware"]["features_present_in_goodware"] == 1
    assert worker.requests[0].tool is ConfinedAnalysisTool.SAMPLE_ANALYSIS
    assert goodware.prepared == baseline_id
    assert goodware.features == (("string", "fixture string"),)
    analysis_observation = next(
        database.observations[reference]
        for reference in result.observation_refs
        if database.observations[reference].kind is EvidenceKind.BINARY_ANALYSIS
    )
    assert analysis_observation.source_version != "unknown"

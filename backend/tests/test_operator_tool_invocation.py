from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

import cti_app.api.investigation as investigation_api
from cti_app.api.investigation import router
from cti_app.application.advisor_checkpoint import (
    advisor_checkpoint_summary,
    format_advisor_trace_purpose,
)
from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.context_pagination import (
    ContextOmissionManifestEntryV1,
    ContextOmissionReason,
    StaleContextCursorError,
    decode_keyset_cursor,
    encode_keyset_cursor,
    verify_page_sha256,
)
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_RESPONSE_SCHEMA,
    ADVISOR_SCHEMA_VERSION,
    InvestigationAdvisorTool,
    register_investigation_advisor_tool,
)
from cti_app.application.investigation_experiments import (
    EXPERIMENT_RECORD_TOOL,
    HYPOTHESIS_DECLARE_TOOL,
    InMemoryInvestigationExperimentRepository,
    register_operator_experiment_tools,
)
from cti_app.application.investigation_tools import (
    ToolResult,
    _authorize_framework_refusal_proof_adapter,
)
from cti_app.application.operator_advisor_guidance import build_advisor_guidance
from cti_app.application.operator_context_summary import InvestigationOperatorContextSummaryV1
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
    OperatorToolInvocationService,
    _canonical_context_content_bytes,
)
from cti_app.application.research_context_builder import CanonicalEvidenceSummaryPort
from cti_app.config import Settings
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    EvidenceObservation,
    FrameworkPreSubmissionProof,
    InvestigationBudget,
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.investigation_experiment import (
    EXPERIMENT_TOOL_SCHEMA_VERSION,
    HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
)
from cti_app.domain.investigation_state import Hypothesis
from tests.test_investigation_service import _Fixture


def _trace(invocation):
    summary = advisor_checkpoint_summary(invocation)
    assert summary is not None
    return summary


def _service(
    fixture: _Fixture,
    *,
    observations=None,
    evidence_summaries=None,
) -> OperatorToolInvocationService:
    return OperatorToolInvocationService(
        investigation_id=fixture.investigation_id,
        investigations=fixture.investigations,
        states=fixture.states,
        envelopes=fixture.envelopes,
        invocations=fixture.invocations,
        context_builder=fixture.context_builder,
        execution=fixture.service,
        registry=fixture.registry,
        available_capabilities=fixture.available_capabilities,
        unavailable_tool_reasons={},
        observations=observations,
        evidence_summaries=evidence_summaries,
    )


async def _fixture(
    *, external_allowed: bool = True
) -> tuple[_Fixture, object, OperatorToolInvocationService]:
    fixture = _Fixture(
        responses=[],
        external_allowed=external_allowed,
        tools=[("vt.file_report", "vt")],
    )
    state = await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    return fixture, state, _service(fixture)


@pytest.mark.asyncio
async def test_advisor_validation_rejection_is_json_serializable_in_api_response(monkeypatch):
    investigation_id = uuid4()
    invocation_id = uuid4()
    captured = ToolResult(
        ToolInvocationStatus.NEEDS_REVIEW,
        error_code="advisor_output_invalid",
        output={
            "advisor_status": "output_invalid",
            "validation_rejection": {
                "code": "object_required",
                "path": "$.recommended_next_tools[0].args_hint",
            },
            "response_excerpt": '{"args_hint":"--limit 3"}',
            "response_sha256": "a" * 64,
            "response_bytes": 24,
        },
    )
    invocation = SimpleNamespace(
        operation_key="advisor-api-serialization-test",
        id=invocation_id,
        tool_name="advisor.consult",
        schema_version=ADVISOR_SCHEMA_VERSION,
        status=ToolInvocationStatus.NEEDS_REVIEW,
        version=1,
        response_refs=(),
    )
    result = SimpleNamespace(
        investigation_id=investigation_id,
        working_state_version=2,
        idempotent_replay=False,
        invocation=invocation,
        error_code="invocation_needs_review",
        job_id=None,
        output={"kind": "advisor_consult", "status": "needs_review", "data": {}},
    )

    class _Uow:
        class _Investigations:
            async def get(self, _investigation_id):
                return object()

        analyst_investigations = _Investigations()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return None

    class _OperatorInvocations:
        async def invoke(self, **_kwargs):
            return result

    advisor = object.__new__(InvestigationAdvisorTool)
    advisor._results = {invocation.operation_key: captured}

    composition = SimpleNamespace(
        operator_tool_invocations=_OperatorInvocations(),
        advisor_tool=advisor,
    )

    monkeypatch.setattr(investigation_api, "_uow_factory", lambda _request: _Uow)
    monkeypatch.setattr(
        investigation_api,
        "_composition_factory",
        lambda _request: SimpleNamespace(build=lambda *_args, **_kwargs: composition),
    )

    async def allow_operator_client(_request):
        return None

    monkeypatch.setattr(investigation_api, "_require_operator_client", allow_operator_client)
    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/investigations/{investigation_id}/operator/tools/advisor.consult",
            headers={"Idempotency-Key": "advisor-api-serialization-test"},
            json={
                "schema_version": ADVISOR_SCHEMA_VERSION,
                "arguments": {"consultation_type": "plan_investigation"},
                "purpose": "Check bounded advisor serialization.",
                "expected_version": 1,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["advisor_output"]["validation_rejection"] == {
        "code": "object_required",
        "path": "$.recommended_next_tools[0].args_hint",
    }


@pytest.mark.asyncio
async def test_same_key_replays_durable_result_and_changed_payload_conflicts() -> None:
    fixture, state, service = await _fixture()
    request = {
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "arguments": {"query": "sample hash"},
        "purpose": "verify the sample report",
        "expected_version": state.version,
        "idempotency_key": "operator-key-1",
    }

    first = await service.invoke(**request)
    after_first = await fixture.states.get(fixture.investigation_id)
    replay = await service.invoke(**request)

    assert first.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert first.output["untrusted_data"] is True
    assert first.output["kind"] == "vt_file_report"
    assert first.output["data"]["result"]["summary"] == (
        "vt.file_report observed the requested relation."
    )
    assert replay.idempotent_replay is True
    assert replay.invocation.id == first.invocation.id
    assert first.working_state_version == state.version + 1
    assert after_first is not None
    assert after_first.version == state.version + 1
    assert (await fixture.states.get(fixture.investigation_id)).version == after_first.version
    assert fixture.tools["vt.file_report"].calls == [{"query": "sample hash"}]
    assert first.invocation.operation_key in fixture.committed_running_keys
    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(**{**request, "purpose": "different payload"})
    assert error.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_hypothesis_declare_uses_one_cas_and_is_idempotent() -> None:
    fixture, state, _ = await _fixture()
    fixture.available_capabilities = frozenset((*fixture.available_capabilities, "operator.write"))
    fixture.service._available_capabilities = fixture.available_capabilities
    experiment_repository = InMemoryInvestigationExperimentRepository()
    register_operator_experiment_tools(
        fixture.registry,
        experiments=experiment_repository,
        observations=fixture.observations,
        invocations=fixture.invocations,
        states=fixture.states,
        envelopes=fixture.envelopes,
        article_snapshots=fixture.context_builder.article_snapshots,
    )
    service = _service(fixture)
    request = {
        "tool_name": HYPOTHESIS_DECLARE_TOOL,
        "schema_version": "1",
        "arguments": {
            "hypothesis_id": "H-S3P2.1",
            "statement": "PsProxy delegates retrieval through the observed proxy stage.",
        },
        "purpose": "Record the operator's falsifiable S3P2.1 hypothesis.",
        "expected_version": state.version,
        "idempotency_key": "hypothesis-s3p2-declare-1",
    }

    first = await service.invoke(**request)
    after_first = await fixture.states.get(fixture.investigation_id)
    replay = await service.invoke(**request)

    assert first.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert first.working_state_version == state.version + 1
    assert after_first is not None
    assert after_first.version == state.version + 1
    assert after_first.hypotheses == (
        Hypothesis(
            id="H-S3P2.1",
            statement="PsProxy delegates retrieval through the observed proxy stage.",
        ),
    )
    assert replay.idempotent_replay is True
    assert replay.invocation.id == first.invocation.id
    assert (await fixture.states.get(fixture.investigation_id)).version == after_first.version

    same_content = await service.invoke(
        tool_name=HYPOTHESIS_DECLARE_TOOL,
        schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
        arguments=request["arguments"],
        purpose=request["purpose"],
        expected_version=after_first.version,
        idempotency_key="hypothesis-s3p2-declare-2",
    )
    after_same_content = await fixture.states.get(fixture.investigation_id)
    assert same_content.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert after_same_content is not None
    assert after_same_content.version == after_first.version + 1
    assert after_same_content.hypotheses == after_first.hypotheses

    conflicting_content = await service.invoke(
        tool_name=HYPOTHESIS_DECLARE_TOOL,
        schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
        arguments={
            **request["arguments"],
            "statement": "A conflicting statement must not replace the declaration.",
        },
        purpose=request["purpose"],
        expected_version=after_same_content.version,
        idempotency_key="hypothesis-s3p2-conflicting-content",
    )
    after_conflict = await fixture.states.get(fixture.investigation_id)
    assert conflicting_content.invocation.status is ToolInvocationStatus.FAILED
    assert after_conflict is not None
    assert after_conflict.hypotheses == after_same_content.hypotheses


@pytest.mark.asyncio
async def test_hypothesis_declare_without_current_g2_does_not_append_hypothesis() -> None:
    fixture, state, _ = await _fixture()
    fixture.available_capabilities = frozenset((*fixture.available_capabilities, "operator.write"))
    fixture.service._available_capabilities = fixture.available_capabilities
    register_operator_experiment_tools(
        fixture.registry,
        experiments=InMemoryInvestigationExperimentRepository(),
        observations=fixture.observations,
        invocations=fixture.invocations,
        states=fixture.states,
        envelopes=fixture.envelopes,
        article_snapshots=fixture.context_builder.article_snapshots,
    )
    fixture.envelopes._items.pop(fixture.investigation_id)
    service = _service(fixture)

    result = await service.invoke(
        tool_name=HYPOTHESIS_DECLARE_TOOL,
        schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
        arguments={"hypothesis_id": "H-no-g2", "statement": "A bounded hypothesis."},
        purpose="Try a declaration without an approved G2 envelope.",
        expected_version=state.version,
        idempotency_key="hypothesis-no-g2",
    )

    saved_state = await fixture.states.get(fixture.investigation_id)
    assert result.invocation.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output["data"]["result"]["error_code"] == "g2_approval_required"
    assert saved_state is not None
    assert saved_state.version == state.version + 1
    assert saved_state.hypotheses == ()


@pytest.mark.asyncio
async def test_hypothesis_declare_does_not_append_after_g2_changes() -> None:
    fixture, state, _ = await _fixture()
    fixture.available_capabilities = frozenset((*fixture.available_capabilities, "operator.write"))
    fixture.service._available_capabilities = fixture.available_capabilities
    original_repository = fixture.envelopes
    approved_envelope = await original_repository.get(fixture.investigation_id)
    assert approved_envelope is not None

    class _ChangingEnvelopeRepository:
        def __init__(self) -> None:
            self.reads = 0

        async def get(self, investigation_id):
            self.reads += 1
            if self.reads >= 3:
                return replace(approved_envelope, approval_decision_id=uuid4())
            return await original_repository.get(investigation_id)

        async def save_if_version(self, envelope, *, expected_version):
            return await original_repository.save_if_version(
                envelope, expected_version=expected_version
            )

    changing_repository = _ChangingEnvelopeRepository()
    fixture.envelopes = changing_repository
    fixture.service._envelopes = changing_repository
    register_operator_experiment_tools(
        fixture.registry,
        experiments=InMemoryInvestigationExperimentRepository(),
        observations=fixture.observations,
        invocations=fixture.invocations,
        states=fixture.states,
        envelopes=changing_repository,
        article_snapshots=fixture.context_builder.article_snapshots,
    )

    result = await _service(fixture).invoke(
        tool_name=HYPOTHESIS_DECLARE_TOOL,
        schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
        arguments={"hypothesis_id": "H-S3P2.1", "statement": "A bounded hypothesis."},
        purpose="Reject a declaration after its G2 envelope changes.",
        expected_version=state.version,
        idempotency_key="hypothesis-stale-g2",
    )

    saved_state = await fixture.states.get(fixture.investigation_id)
    assert result.invocation.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output["data"]["result"]["error_code"] == "g2_envelope_changed"
    assert saved_state is not None
    assert saved_state.hypotheses == ()


@pytest.mark.asyncio
async def test_hypothesis_declare_rejects_uncontrolled_ids_without_state_append() -> None:
    fixture, state, _ = await _fixture()
    fixture.available_capabilities = frozenset((*fixture.available_capabilities, "operator.write"))
    fixture.service._available_capabilities = fixture.available_capabilities
    register_operator_experiment_tools(
        fixture.registry,
        experiments=InMemoryInvestigationExperimentRepository(),
        observations=fixture.observations,
        invocations=fixture.invocations,
        states=fixture.states,
        envelopes=fixture.envelopes,
        article_snapshots=fixture.context_builder.article_snapshots,
    )

    result = await _service(fixture).invoke(
        tool_name=HYPOTHESIS_DECLARE_TOOL,
        schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
        arguments={"hypothesis_id": "H invalid/id", "statement": "A bounded hypothesis."},
        purpose="Reject an uncontrolled hypothesis identifier.",
        expected_version=state.version,
        idempotency_key="hypothesis-invalid-id",
    )

    saved_state = await fixture.states.get(fixture.investigation_id)
    assert result.invocation.status is ToolInvocationStatus.FAILED
    assert saved_state is not None
    assert saved_state.version == state.version + 1
    assert saved_state.hypotheses == ()


@pytest.mark.asyncio
async def test_hypothesis_declare_requires_operator_write_capability() -> None:
    fixture, state, _ = await _fixture()
    fixture.available_capabilities = frozenset(
        item for item in fixture.available_capabilities if item != "operator.write"
    )
    fixture.service._available_capabilities = fixture.available_capabilities
    register_operator_experiment_tools(
        fixture.registry,
        experiments=InMemoryInvestigationExperimentRepository(),
        observations=fixture.observations,
        invocations=fixture.invocations,
        states=fixture.states,
        envelopes=fixture.envelopes,
        article_snapshots=fixture.context_builder.article_snapshots,
    )

    result = await _service(fixture).invoke(
        tool_name=HYPOTHESIS_DECLARE_TOOL,
        schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
        arguments={"hypothesis_id": "H-S3P2.1", "statement": "A bounded hypothesis."},
        purpose="Reject a declaration without operator.write.",
        expected_version=state.version,
        idempotency_key="hypothesis-without-operator-write",
    )

    saved_state = await fixture.states.get(fixture.investigation_id)
    assert result.invocation.status is ToolInvocationStatus.UNAVAILABLE
    assert "operator.write" in result.output["error"]["reason"]
    assert saved_state is not None
    assert saved_state.hypotheses == ()


@pytest.mark.asyncio
async def test_operator_led_experiment_record_uses_declared_hypothesis_and_stream_cas() -> None:
    fixture, state, _ = await _fixture()
    fixture.available_capabilities = frozenset((*fixture.available_capabilities, "operator.write"))
    fixture.service._available_capabilities = fixture.available_capabilities
    experiment_repository = InMemoryInvestigationExperimentRepository()
    register_operator_experiment_tools(
        fixture.registry,
        experiments=experiment_repository,
        observations=fixture.observations,
        invocations=fixture.invocations,
        states=fixture.states,
        envelopes=fixture.envelopes,
        article_snapshots=fixture.context_builder.article_snapshots,
    )
    service = _service(fixture)
    declared = await service.invoke(
        tool_name=HYPOTHESIS_DECLARE_TOOL,
        schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
        arguments={
            "hypothesis_id": "H-S3P2.1",
            "statement": "PsProxy uses the measured proxy stage.",
        },
        purpose="Declare a hypothesis before recording its test.",
        expected_version=state.version,
        idempotency_key="hypothesis-before-experiment",
    )
    assert declared.invocation.status is ToolInvocationStatus.SUCCEEDED

    observation = EvidenceObservation(
        investigation_id=fixture.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="b" * 64,
        source="network.capture.inspect",
        source_version="network-capture-inspect.v1",
        observed_at=datetime.now(UTC),
        object_type="network_capture",
        object_id="capture-sha256",
    )
    await fixture.observations.append_if_absent(observation)
    started_at = datetime.now(UTC) - timedelta(seconds=2)
    arguments = {
        "expected_version": 0,
        "hypothesis_id": "H-S3P2.1",
        "claim_ids": ["C1"],
        "prediction": "The persisted capture contains the proxy marker.",
        "falsification_criterion": "A complete relevant capture contains no proxy marker.",
        "method": "Passively inspect the persisted capture with the pinned parser.",
        "method_mode": "passive",
        "input_type": "capture_sha256",
        "input_refs": [{"input_type": "capture_sha256", "input_id": "a" * 64, "sha256": "a" * 64}],
        "preconditions": ["The capture is persisted and bound to this investigation."],
        "availability": "available",
        "tool_name": "network.capture.inspect",
        "tool_version": "4.4.1",
        "tool_schema_version": "network-capture-inspect.v1",
        "tool_arguments": {"max_packets": 500},
        "step_ids": ["step-s3p2-1"],
        "tool_invocation_refs": [],
        "observation_refs": [{"observation_id": str(observation.id), "sha256": observation.sha256}],
        "expected": "The proxy marker is present in the relevant capture window.",
        "actual": "The proxy marker is present in the persisted capture.",
        "actual_outcome": "observed",
        "observation_coverage": "complete",
        "started_at": started_at.isoformat(),
        "completed_at": (started_at + timedelta(seconds=1)).isoformat(),
        "scope": "Static capture inspection; no victim execution is established.",
        "limitations": [],
        "verdict": "supports",
        "implication": "The capture supports the bounded proxy-stage claim.",
        "next_pivots": ["c" * 64],
    }

    result = await service.invoke(
        tool_name=EXPERIMENT_RECORD_TOOL,
        schema_version=EXPERIMENT_TOOL_SCHEMA_VERSION,
        arguments=arguments,
        purpose="Record the experiment against persisted S3P2.1 evidence.",
        expected_version=declared.working_state_version,
        idempotency_key="experiment-s3p2-record-1",
    )

    saved = await experiment_repository.list_for_investigation(fixture.investigation_id)
    assert result.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert len(saved) == 1
    assert saved[0].version == 1
    assert saved[0].hypothesis_id == "H-S3P2.1"
    assert saved[0].observation_refs[0].observation_id == observation.id
    assert saved[0].observation_refs[0].sha256 == observation.sha256


@pytest.mark.asyncio
async def test_operator_dispatcher_sends_advisor_when_source_flags_are_false():
    fixture = _Fixture(responses=[], tools=[("vt.file_report", "vt")])
    state = await fixture.start(
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
        hypotheses=(
            Hypothesis(
                id="H-loader-chain",
                statement="The article's loader chain may explain the observed pivot.",
            ),
        ),
    )
    article_port = fixture.context_builder.article_snapshots
    article_port.snapshot = replace(
        article_port.snapshot,
        do_not_submit=True,
        external_llm_allowed=False,
        article_passages=(
            SimpleNamespace(
                claim_id="S1P1",
                heading="Loader chain",
                text="S1P1. The article describes a loader chain.",
            ),
        ),
    )
    # Source flags no longer veto Bridge; the human G2 envelope disclosure
    # (left permissive here) remains a veto, covered by the PG egress test.
    fixture.envelope = replace(fixture.envelope, allowed_providers=("bridge",))
    fixture.envelopes._items[fixture.investigation_id] = fixture.envelope
    fixture.available_capabilities = frozenset({"bridge.advisor"})
    fixture.service._available_capabilities = fixture.available_capabilities

    class ArtifactStore:
        def __init__(self):
            self.values = {}

        async def put_canonical_json(self, payload, *, bucket):
            raw = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            blob_id = uuid4()
            self.values[blob_id] = raw
            return blob_id, hashlib.sha256(raw).hexdigest()

        async def read_bytes(self, blob_id, *, max_bytes):
            raw = self.values[blob_id]
            assert len(raw) <= max_bytes
            return raw

        async def put_text(self, value, *, bucket):
            return await self.put_bytes(
                value.encode("utf-8"), bucket=bucket, mime_type="text/plain"
            )

        async def put_bytes(self, value, *, bucket, mime_type):
            blob_id = uuid4()
            self.values[blob_id] = bytes(value)
            return blob_id

    class StrictBridge:
        def __init__(self):
            self.calls = []

        async def submit(self, **kwargs):
            assert set(kwargs) == {
                "investigation_id",
                "subject_id",
                "turn_index",
                "context_sha256",
                "prompt",
                "external_llm_allowed",
                "timeout_seconds",
            }
            assert kwargs["external_llm_allowed"] is True
            assert isinstance(kwargs["timeout_seconds"], int | float)
            assert not isinstance(kwargs["timeout_seconds"], bool)
            assert 0 < kwargs["timeout_seconds"] <= 3_600
            assert len(kwargs["prompt"].encode()) <= 32 * 1024 * 1024
            self.calls.append(kwargs)
            article_ref = next(
                line[1:].split("]", 1)[0]
                for line in kwargs["prompt"].splitlines()
                if line.startswith("[E")
            )
            return TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=json.dumps(
                    {
                        "schema": ADVISOR_RESPONSE_SCHEMA,
                        "summary": "Comparer les faits mesurés avant un nouveau pivot.",
                        "alternative_hypotheses": [
                            {
                                "statement": "Une source distincte peut expliquer le même signal.",
                                "basis_refs": [article_ref],
                            }
                        ],
                        "discriminating_experiment": {
                            "question": "Quel résultat sépare ces pistes ?",
                            "tool": None,
                            "schema_version": None,
                            "expected_signal": "Un signal différent dans la mesure.",
                            "basis_refs": [article_ref],
                        },
                        "discarded_lead": {
                            "lead": "La piste non discriminante.",
                            "reason": "Elle ne sépare pas les hypothèses.",
                            "basis_refs": [article_ref],
                        },
                        "recommended_next_tools": [],
                        "risks": ["Une suggestion ne constitue pas une preuve."],
                        "questions": [],
                        "untrusted": True,
                    },
                    ensure_ascii=False,
                ),
            )

    async def empty_associations(_investigation_id):
        return ()

    async def empty_samples(_subject_id):
        return ()

    transport = StrictBridge()
    artifact_store = ArtifactStore()
    advisor = InvestigationAdvisorTool(
        transport=transport,
        settings=Settings(
            openai_bridge_min_submission_interval_seconds=0,
            openai_bridge_submission_jitter_fraction=0,
        ),
        investigations=fixture.investigations,
        investigation_states=fixture.states,
        observations=fixture.observations,
        findings=fixture.findings,
        opinions=fixture.opinions,
        sample_associations=SimpleNamespace(list_for_investigation=empty_associations),
        samples=SimpleNamespace(list_for_subject=empty_samples),
        blobs=SimpleNamespace(),
        article_snapshots=fixture.context_builder.article_snapshots,
        artifact_store=artifact_store,
        registry=fixture.registry,
    )
    register_investigation_advisor_tool(fixture.registry, adapter=advisor)
    fixture.registry.set_availability_catalog((), {})
    service = _service(fixture)

    result = await service.invoke(
        tool_name="advisor.consult",
        schema_version=ADVISOR_SCHEMA_VERSION,
        arguments={"consultation_type": "plan_investigation", "question": "S1P1"},
        purpose="Orienter le prochain pivot a partir de l'etat mesure.",
        expected_version=state.version,
        idempotency_key="operator-advisor-minimal-e2e",
    )

    advisor_result = result.output["data"]["result"]
    assert result.invocation.status is ToolInvocationStatus.SUCCEEDED, {
        key: advisor_result.get(key)
        for key in (
            "advisor_status",
            "failure_stage",
            "failure_reason",
            "bridge_turns_consumed",
            "error_code",
        )
    }
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert "DOSSIER DE FAITS" in prompt
    assert "Article S1P1" in prompt
    assert "UNTRUSTED_ADVISORY_INPUT" not in prompt
    assert "article:" not in prompt
    refs = [await fixture.observations.get(ref) for ref in result.invocation.response_refs]
    advisor_records = [
        item for item in refs if item is not None and item.source == "advisor.consult"
    ]
    assert advisor_records
    assert result.output["data"]["result"]["summary"].startswith("Comparer")
    assert result.output["data"]["result"]["schema"] == ADVISOR_RESPONSE_SCHEMA


@pytest.mark.asyncio
async def test_advisor_recommendations_record_acceptance_refusal_and_empirical_outcomes() -> None:
    fixture = _Fixture(
        responses=[],
        tools=(
            ("vt.file_report", "vt"),
            ("vt.object_report", "vt"),
            ("sample.analyze", "local"),
        ),
    )
    state = await fixture.start(
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
        hypotheses=(
            Hypothesis(
                id="H-init-mutex",
                statement="The measured InitMutex may identify a sample-specific loader.",
            ),
        ),
    )
    fixture.envelope = replace(
        fixture.envelope,
        allowed_providers=("bridge", "local", "vt"),
    )
    fixture.envelopes._items[fixture.investigation_id] = fixture.envelope
    fixture.available_capabilities = frozenset((*fixture.available_capabilities, "bridge.advisor"))
    fixture.service._available_capabilities = fixture.available_capabilities

    class ArtifactStore:
        def __init__(self):
            self.values = {}

        async def put_canonical_json(self, payload, *, bucket):
            raw = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            blob_id = uuid4()
            self.values[blob_id] = raw
            return blob_id, hashlib.sha256(raw).hexdigest()

        async def read_bytes(self, blob_id, *, max_bytes):
            raw = self.values[blob_id]
            assert len(raw) <= max_bytes
            return raw

        async def put_text(self, value, *, bucket):
            return await self.put_bytes(
                value.encode("utf-8"), bucket=bucket, mime_type="text/plain"
            )

        async def put_bytes(self, value, *, bucket, mime_type):
            blob_id = uuid4()
            self.values[blob_id] = bytes(value)
            return blob_id

    artifacts = ArtifactStore()

    def add_measurement(source: str, sample_hash: str, value: str) -> EvidenceObservation:
        raw = json.dumps(
            {
                "schema": "sample-obfuscated-strings.v1",
                "sample_sha256": sample_hash,
                "strings": [{"value": value, "offset": 128}],
            },
            separators=(",", ":"),
        ).encode()
        blob_id = uuid4()
        artifacts.values[blob_id] = raw
        return EvidenceObservation(
            investigation_id=fixture.investigation_id,
            kind=EvidenceKind.BINARY_STRING,
            blob_id=blob_id,
            sha256=hashlib.sha256(raw).hexdigest(),
            source=source,
            source_version="fixture-extractor.v1",
            observed_at=datetime.now(UTC),
            object_type="sample",
            object_id=sample_hash,
        )

    initial_observation = add_measurement(
        "sample.extract_obfuscated_strings", "a" * 64, "InitMutex"
    )
    accepted_observation = add_measurement("vt.file_report", "a" * 64, "RemoteConfig")
    alternative_observation = add_measurement("sample.analyze", "a" * 64, "LoaderFunction")
    for item in (initial_observation, accepted_observation, alternative_observation):
        await fixture.observations.append_if_absent(item)

    async def get_verified_blob(blob_id):
        observation = next(
            (
                item
                for item in (initial_observation, accepted_observation, alternative_observation)
                if item.blob_id == blob_id
            ),
            None,
        )
        raw = artifacts.values.get(blob_id)
        if observation is None or raw is None:
            return None
        return SimpleNamespace(descriptor=SimpleNamespace(sha256=observation.sha256, size=len(raw)))

    summaries = CanonicalEvidenceSummaryPort(artifacts)

    class StrictBridge:
        def __init__(self):
            self.calls = []

        async def submit(self, **kwargs):
            self.calls.append(kwargs)
            basis = [str(initial_observation.id)]
            return TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=json.dumps(
                    {
                        "schema": ADVISOR_RESPONSE_SCHEMA,
                        "summary": "Comparer deux pivots mesurables.",
                        "alternative_hypotheses": [
                            {
                                "statement": "Une autre famille peut partager le signal.",
                                "basis_refs": basis,
                            }
                        ],
                        "discriminating_experiment": {
                            "question": "Le second échantillon porte-t-il le même mutex ?",
                            "tool": "sample.analyze",
                            "schema_version": "1",
                            "expected_signal": (
                                "La fonction est mesurée dans le second échantillon."
                            ),
                            "basis_refs": basis,
                        },
                        "discarded_lead": {
                            "lead": "Le nom de fichier fournisseur.",
                            "reason": "Il ne distingue pas les variantes.",
                            "basis_refs": basis,
                        },
                        "recommended_next_tools": [
                            {
                                "tool": "vt.file_report",
                                "schema_version": "1",
                                "why": "Vérifier la fiche du hash déjà mesuré.",
                                "expected_signal": "La fiche contient la même configuration.",
                                "basis_refs": basis,
                            },
                            {
                                "tool": "vt.object_report",
                                "schema_version": "1",
                                "why": "Examiner l'objet relié à la première observation.",
                                "args_hint": {"query": "a" * 64},
                                "expected_signal": (
                                    "La relation fournit un second identifiant mesuré."
                                ),
                                "basis_refs": basis,
                            },
                        ],
                        "risks": [],
                        "questions": [],
                        "untrusted": True,
                    },
                    ensure_ascii=False,
                ),
            )

    async def empty_associations(_investigation_id):
        return ()

    async def empty_samples(_subject_id):
        return ()

    transport = StrictBridge()
    advisor = InvestigationAdvisorTool(
        transport=transport,
        settings=Settings(
            openai_bridge_min_submission_interval_seconds=0,
            openai_bridge_submission_jitter_fraction=0,
        ),
        investigations=fixture.investigations,
        investigation_states=fixture.states,
        observations=fixture.observations,
        findings=fixture.findings,
        opinions=fixture.opinions,
        sample_associations=SimpleNamespace(list_for_investigation=empty_associations),
        samples=SimpleNamespace(list_for_subject=empty_samples),
        blobs=SimpleNamespace(get=get_verified_blob),
        article_snapshots=fixture.context_builder.article_snapshots,
        artifact_store=artifacts,
        registry=fixture.registry,
    )
    register_investigation_advisor_tool(fixture.registry, adapter=advisor)
    service = _service(
        fixture,
        observations=fixture.observations,
        evidence_summaries=summaries,
    )

    advice = await service.invoke(
        tool_name="advisor.consult",
        schema_version=ADVISOR_SCHEMA_VERSION,
        arguments={
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(initial_observation.id)],
        },
        purpose="Comparer les pivots mesurés.",
        expected_version=state.version,
        idempotency_key="advisor-recommendation-source",
    )
    persisted_advice = [
        item
        for item in await fixture.observations.list_for_investigation(fixture.investigation_id)
        if item.source == "advisor.consult"
    ]
    assert advice.invocation.status is ToolInvocationStatus.PARTIAL, advice.output
    assert len(transport.calls) == 1
    assert persisted_advice
    assert advice.invocation.consumed_budget[BudgetDimension.BRIDGE_TURNS] == 1
    advisor_observation = next(
        item for item in persisted_advice if item.id in advice.invocation.response_refs
    )
    advisor_summary = await summaries.summary(advisor_observation)
    assert advisor_summary is not None
    assert advisor_summary["schema"] == "operator-advisor-call.v1"
    assert advisor_summary["consultation_type"] == "pivot_strategy"
    assert isinstance(advisor_summary["input_observation_refs"], list)
    projected_output = advisor_summary["output"]
    assert isinstance(projected_output["alternative_hypotheses"], list)
    assert isinstance(projected_output["basis_observation_refs"], list)
    assert isinstance(projected_output["recommended_next_tools"], list)
    assert isinstance(projected_output["risks"], list)
    assert isinstance(projected_output["questions"], list)
    assert advisor_summary["output"]["normalizations"] == [
        "$.recommended_next_tools[0].args_hint:absent_to_empty_object"
    ]
    assert len(canonical_json_bytes(advisor_summary)) <= 48 * 1024
    guidance = await build_advisor_guidance(
        invocations=(advice.invocation,),
        observations=(advisor_observation,),
        evidence_summaries=summaries,
        available_tools=("vt.file_report", "vt.object_report", "sample.analyze"),
    )
    assert guidance["last_advice"] is not None
    assert guidance["last_advice"]["invocation_id"] == advice.invocation.id
    assert guidance["last_advice"]["consultation_type"] == "pivot_strategy"
    assert isinstance(guidance["last_advice"]["date"], datetime)
    assert isinstance(guidance["last_advice"]["key_points"], tuple)
    advice_trace = _trace(advice.invocation)
    assert "checkpoint " in advice_trace
    assert " consulted" in advice_trace
    assert "cost=turns:1" in advice_trace
    assert "provenance=" in advice_trace

    fixture.tools["vt.file_report"].result = ToolResult(
        status=ToolInvocationStatus.SUCCEEDED,
        observation_refs=(accepted_observation.id,),
        output={"summary": "La fiche confirme une seconde valeur mesurée."},
    )
    latest_state = await fixture.states.get(fixture.investigation_id)
    accepted = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "a" * 64},
        purpose=format_advisor_trace_purpose(
            "Exécuter le pivot choisi.",
            recommendation={
                "invocation_id": str(advice.invocation.id),
                "index": 0,
                "decision": "accepted",
            },
        ),
        expected_version=latest_state.version,
        idempotency_key="advisor-recommendation-accepted",
    )

    fixture.tools["sample.analyze"].result = ToolResult(
        status=ToolInvocationStatus.SUCCEEDED,
        observation_refs=(alternative_observation.id,),
        output={"summary": "Une mesure alternative est disponible."},
    )
    latest_state = await fixture.states.get(fixture.investigation_id)
    refused = await service.invoke(
        tool_name="sample.analyze",
        schema_version="1",
        arguments={"query": "alternative"},
        purpose=format_advisor_trace_purpose(
            "Choisir une autre analyse.",
            recommendation={
                "invocation_id": str(advice.invocation.id),
                "index": 1,
                "decision": "refused",
                "reason": "Le pivot VT ne discrimine pas assez les hypothèses.",
            },
        ),
        expected_version=latest_state.version,
        idempotency_key="advisor-recommendation-refused",
    )

    assert accepted.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert accepted_observation.id in accepted.invocation.response_refs
    assert "recommendation" in _trace(accepted.invocation)
    assert "accepted" in _trace(accepted.invocation)
    assert "empirical_refs=" in _trace(accepted.invocation)
    assert refused.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert alternative_observation.id in refused.invocation.response_refs
    assert "refused" in _trace(refused.invocation)
    assert "Le pivot VT ne discrimine pas assez" in _trace(refused.invocation)

    current_state = await fixture.states.get(fixture.investigation_id)
    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(
        service, fixture=fixture, artifact_store=artifacts
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        operator_status = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator-status"
        )
        operator_context = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context",
            params={
                "view": "summary",
                "expected_version": current_state.version,
                "expected_sha256": current_state.sha256,
            },
        )

    assert operator_status.status_code == 200, operator_status.text
    last_advice = operator_status.json()["advisor_guidance"]["last_advice"]
    assert last_advice is not None
    assert last_advice["consultation_type"] == "pivot_strategy"
    assert isinstance(datetime.fromisoformat(last_advice["date"]), datetime)
    assert isinstance(last_advice["key_points"], list)
    assert last_advice["key_points"]
    assert last_advice["invocation_id"] == str(advice.invocation.id)
    assert operator_context.status_code == 200, operator_context.text
    assert operator_context.json()["schema"] == "investigation-operator-context-summary.v1"

    fixture.tools["sample.analyze"].result = ToolResult(
        status=ToolInvocationStatus.SUCCEEDED,
        observation_refs=(alternative_observation.id,),
        output={"summary": "La mesure est répétée pour confirmer le résultat."},
    )
    latest_state = await fixture.states.get(fixture.investigation_id)
    continued = await service.invoke(
        tool_name="sample.analyze",
        schema_version="1",
        arguments={"query": "repeat-observation"},
        purpose="Répéter localement la mesure avant de conclure.",
        expected_version=latest_state.version,
        idempotency_key="advisor-checkpoint-skipped-by-operator",
    )
    assert continued.invocation.status is ToolInvocationStatus.SUCCEEDED
    continued_trace = _trace(continued.invocation)
    assert "checkpoint first_significant_decompilation@" in continued_trace
    assert " skipped" in continued_trace
    assert "operator_proceeded_without_advisor" in continued_trace
    assert "cost=" in continued_trace
    assert "provenance=" in continued_trace
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_stale_version_unknown_tool_and_invalid_schema_are_stable() -> None:
    fixture, state, service = await _fixture()

    with pytest.raises(OperatorToolInvocationError) as stale:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments={"query": "hash"},
            purpose="check report",
            expected_version=state.version + 1,
            idempotency_key="stale-key",
        )
    assert stale.value.code == "stale_working_state"

    unknown = await service.invoke(
        tool_name="missing.tool",
        schema_version="1",
        arguments={"query": "hash"},
        purpose="check tool lookup",
        expected_version=state.version,
        idempotency_key="unknown-key",
    )
    assert unknown.error_code == "tool_unavailable"
    assert unknown.output["error"]["code"] == "tool_unavailable"
    assert "not registered" in unknown.output["error"]["reason"]

    next_state = await fixture.states.get(fixture.investigation_id)
    invalid = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": 7},
        purpose="exercise schema rejection",
        expected_version=next_state.version,
        idempotency_key="schema-key",
    )
    assert invalid.invocation.status is ToolInvocationStatus.FAILED
    assert fixture.tools["vt.file_report"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["missing_g2", "revoked_g2", "budget_exhausted"])
async def test_g2_and_budget_guards_refuse_external_invocation(policy: str) -> None:
    fixture, state, service = await _fixture()
    if policy == "missing_g2":
        fixture.envelopes._items.pop(fixture.investigation_id)
    elif policy == "revoked_g2":
        envelope = fixture.envelopes._items[fixture.investigation_id]
        fixture.envelopes._items[fixture.investigation_id] = replace(envelope, allowed_providers=())
    else:
        envelope = fixture.envelopes._items[fixture.investigation_id]
        fixture.envelopes._items[fixture.investigation_id] = replace(
            envelope,
            budget=InvestigationBudget(limits={BudgetDimension.VT_UNITS: 0}),
        )

    if policy == "missing_g2":
        with pytest.raises(OperatorToolInvocationError) as denied:
            await service.invoke(
                tool_name="vt.file_report",
                schema_version="1",
                arguments={"query": "hash"},
                purpose="test the current authorization",
                expected_version=state.version,
                idempotency_key=f"policy-{policy}",
            )
        assert denied.value.code == "external_disclosure_denied"
        assert fixture.tools["vt.file_report"].calls == []
        return

    result = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "hash"},
        purpose="test the current authorization",
        expected_version=state.version,
        idempotency_key=f"policy-{policy}",
    )

    expected = (
        ToolInvocationStatus.NEEDS_REVIEW
        if policy == "budget_exhausted"
        else ToolInvocationStatus.POLICY_DENIED
    )
    assert result.invocation.status is expected
    assert fixture.tools["vt.file_report"].calls == []


@pytest.mark.asyncio
async def test_external_llm_disclosure_is_required_for_operator_client() -> None:
    _fixture_value, state, service = await _fixture(external_allowed=False)

    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments={"query": "hash"},
            purpose="external client tool call",
            expected_version=state.version,
            idempotency_key="disclosure-key",
        )
    assert error.value.code == "external_disclosure_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    ("red_tlp", "snapshot_do_not_submit", "envelope_do_not_submit", "external_llm_denied"),
)
async def test_disclosure_controls_block_context_before_external_tool_dispatch(policy: str) -> None:
    fixture, state, service = await _fixture()
    article_port = fixture.context_builder.article_snapshots
    if policy == "red_tlp":
        article_port.snapshot = replace(article_port.snapshot, tlp="RED")
    elif policy == "snapshot_do_not_submit":
        article_port.snapshot = replace(article_port.snapshot, do_not_submit=True)
    else:
        envelope = fixture.envelopes._items[fixture.investigation_id]
        if policy == "envelope_do_not_submit":
            disclosure = replace(envelope.disclosure, do_not_submit=True)
        else:
            disclosure = replace(envelope.disclosure, external_llm_allowed=False)
        fixture.envelopes._items[fixture.investigation_id] = replace(
            envelope, disclosure=disclosure
        )

    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments={"query": "sample hash"},
            purpose="deny dispatch when disclosure policy blocks external sharing",
            expected_version=state.version,
            idempotency_key=f"disclosure-{policy}",
        )

    assert error.value.code == "external_disclosure_denied"
    assert fixture.tools["vt.file_report"].calls == []
    assert await fixture.invocations.list_for_investigation(fixture.investigation_id) == ()


@pytest.mark.asyncio
async def test_secret_fields_and_signed_urls_are_rejected_before_persistence() -> None:
    fixture, state, service = await _fixture()

    for index, arguments in enumerate(
        (
            {"query": "hash", "api_key": "secret-value"},
            {"query": "https://files.example/report?X-Amz-Signature=signed-value"},
        )
    ):
        with pytest.raises(OperatorToolInvocationError) as error:
            await service.invoke(
                tool_name="vt.file_report",
                schema_version="1",
                arguments=arguments,
                purpose="check sensitive payload rejection",
                expected_version=state.version,
                idempotency_key=f"sensitive-key-{index}",
            )
        assert error.value.code == "operator_payload_invalid_or_sensitive"

    assert await fixture.invocations.list_for_investigation(fixture.investigation_id) == ()
    assert fixture.tools["vt.file_report"].calls == []


@pytest.mark.asyncio
async def test_replay_after_persisted_running_marks_review_without_redispatch() -> None:
    fixture, state, service = await _fixture()
    key = "crash-key"
    args = {"query": "hash"}
    purpose = "recover after process crash"
    operation_payload = canonical_json_bytes(
        {
            "tool_name": "vt.file_report",
            "schema_version": "1",
            "arguments": args,
            "purpose": purpose,
            "expected_version": state.version,
        }
    )
    key_digest = hashlib.sha256(f"{fixture.investigation_id}\0{key}".encode()).hexdigest()
    payload_digest = hashlib.sha256(operation_payload).hexdigest()
    operation_key = f"operator:{key_digest}:{payload_digest}"
    running = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key=operation_key,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters=args,
        purpose=purpose,
        reserved_budget={BudgetDimension.VT_UNITS: 1},
    ).transition(ToolInvocationStatus.RUNNING)
    await fixture.invocations.add_if_absent(running)

    result = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=args,
        purpose=purpose,
        expected_version=state.version,
        idempotency_key=key,
    )

    assert result.invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.error_code == "invocation_needs_review"
    assert result.idempotent_replay is True
    assert fixture.tools["vt.file_report"].calls == []


@pytest.mark.asyncio
async def test_legacy_advisor_summary_is_not_framework_pre_submission_proof() -> None:
    fixture, state, _ = await _fixture()
    idempotency_key = "legacy-invalid-advisor-input"
    schema_version = ADVISOR_SCHEMA_VERSION
    arguments = {"consultation_type": "pivot_strategy"}
    purpose = "Recover a locally rejected advisor consultation."
    payload = canonical_json_bytes(
        {
            "tool_name": "advisor.consult",
            "schema_version": schema_version,
            "arguments": arguments,
            "purpose": purpose,
            "expected_version": state.version,
        }
    )
    key_digest = hashlib.sha256(
        f"{fixture.investigation_id}\0{idempotency_key}".encode()
    ).hexdigest()
    operation_key = f"operator:{key_digest}:{hashlib.sha256(payload).hexdigest()}"
    reserved = {BudgetDimension.BRIDGE_TURNS: 1}
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    envelope.budget.reserve(operation_key, reserved)
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )

    observation = EvidenceObservation(
        investigation_id=fixture.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="e" * 64,
        source="advisor.consult",
        source_version="advisor-consult-v1",
        observed_at=datetime.now(UTC),
        object_type="tool_call",
        object_id="advisor.consult",
    )
    await fixture.observations.append_if_absent(observation)
    invocation = ToolInvocation(
        investigation_id=fixture.investigation_id,
        operation_key=operation_key,
        tool_name="advisor.consult",
        schema_version=schema_version,
        normalized_parameters=arguments,
        purpose=purpose,
        reserved_budget=reserved,
        response_refs=(observation.id,),
    )
    invocation = invocation.transition(ToolInvocationStatus.RUNNING).transition(
        ToolInvocationStatus.NEEDS_REVIEW
    )
    await fixture.invocations.add_if_absent(invocation)

    class _LegacyEvidenceSummary:
        async def summary(self, _observation):
            return {"advisor_status": "invalid_input"}

    service = _service(
        fixture,
        observations=fixture.observations,
        evidence_summaries=_LegacyEvidenceSummary(),
    )
    replay = await service.invoke(
        tool_name="advisor.consult",
        schema_version=schema_version,
        arguments=arguments,
        purpose=purpose,
        expected_version=state.version,
        idempotency_key=idempotency_key,
    )

    latest_envelope = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )
    assert replay.idempotent_replay is True
    assert replay.invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert reservation.status.value == "reserved"
    assert latest_envelope.budget.reserved[BudgetDimension.BRIDGE_TURNS] == 1
    assert fixture.bridge_transport.submission_count == 0
    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="advisor.consult",
            schema_version=schema_version,
            arguments=arguments,
            purpose=purpose,
            expected_version=state.version,
            idempotency_key=f"{idempotency_key}-fresh",
        )
    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"


@pytest.mark.asyncio
async def test_persisted_presend_refusal_does_not_block_equivalent_fresh_key_retry() -> None:
    fixture, state, _ = await _fixture()
    arguments = {"query": "source reference retry"}
    purpose = "Retry after a recorded pre-consume refusal."
    operation_key = "operator:legacy-presend-refusal"
    reserved = {BudgetDimension.VT_UNITS: 1}
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    assert envelope is not None
    envelope.budget.reserve(operation_key, reserved)
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )

    payload = canonical_json_bytes(
        {
            "schema": "investigation-tool-observation.v1",
            "tool_name": "vt.file_report",
            "schema_version": "1",
            "operation_key": operation_key,
            "result": {
                "status": "needs_review",
                "outcome": "external_effect_unconfirmed",
                "adapter_output_archived": False,
                "external_effect_claim_id": None,
                "error_code": ("virustotal_external_effect_refused_source_references_mismatch"),
            },
        }
    )
    invocation_id = uuid4()
    blob_id = uuid4()
    fixture.proof_artifacts.store(blob_id, payload)
    observation = EvidenceObservation(
        investigation_id=fixture.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=blob_id,
        sha256=hashlib.sha256(payload).hexdigest(),
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="tool_call",
        object_id=operation_key,
        invocation_ref=invocation_id,
    )
    await fixture.observations.append_if_absent(observation)
    prior = (
        ToolInvocation(
            investigation_id=fixture.investigation_id,
            operation_key=operation_key,
            tool_name="vt.file_report",
            schema_version="1",
            normalized_parameters=arguments,
            purpose=purpose,
            reserved_budget=reserved,
            response_refs=(observation.id,),
            id=invocation_id,
        )
        .transition(ToolInvocationStatus.RUNNING)
        .transition(ToolInvocationStatus.NEEDS_REVIEW)
    )
    await fixture.invocations.add_if_absent(prior)

    service = _service(
        fixture,
        observations=fixture.observations,
        evidence_summaries=fixture.proof_artifacts,
    )
    await fixture.service.resume(state.investigation_id)

    resolved_before_retry = await fixture.invocations.get(prior.id)
    released_envelope = await fixture.envelopes.get(fixture.investigation_id)
    released_reservation = next(
        item
        for item in released_envelope.budget.reservations
        if item.operation_key == operation_key
    )
    assert resolved_before_retry is not None
    assert resolved_before_retry.status is ToolInvocationStatus.FAILED
    assert released_reservation.status.value == "released"

    retried = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose=purpose,
        expected_version=state.version,
        idempotency_key="fresh-presend-refusal-retry",
    )

    resolved = await fixture.invocations.get(prior.id)
    latest_envelope = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )
    assert retried.invocation.id != prior.id
    assert retried.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert resolved is not None
    assert resolved.status is ToolInvocationStatus.FAILED
    assert reservation.status.value == "released"
    assert fixture.tools["vt.file_report"].calls == [arguments]


@pytest.mark.asyncio
async def test_unconfirmed_external_result_still_blocks_equivalent_fresh_key_retry() -> None:
    fixture, state, service = await _fixture()
    arguments = {"query": "ambiguous external effect"}
    fixture.tools["vt.file_report"].result = ToolResult(
        status=ToolInvocationStatus.NEEDS_REVIEW,
        output={"outcome": "external_effect_unconfirmed", "adapter_output_archived": False},
    )

    first = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose="Perform the possibly handed-off read.",
        expected_version=state.version,
        idempotency_key="ambiguous-first-attempt",
    )

    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments=arguments,
            purpose="Perform the possibly handed-off read.",
            expected_version=first.working_state_version,
            idempotency_key="ambiguous-fresh-key",
        )

    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    assert fixture.tools["vt.file_report"].calls == [arguments]


@pytest.mark.asyncio
async def test_proven_presend_refusal_releases_budget_and_allows_new_invocation() -> None:
    fixture, state, service = await _fixture()
    arguments = {"query": "typed pre-consume refusal"}
    purpose = "Retry after the coordinator refused before consume."
    fixture.tools["vt.file_report"].result = ToolResult(
        status=ToolInvocationStatus.POLICY_DENIED,
        output={"reason": "external_effect_refused"},
        error_code="virustotal_external_effect_refused_source_references_mismatch",
        framework_pre_submission_proof=(
            FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
        ),
    )
    _authorize_framework_refusal_proof_adapter(fixture.tools["vt.file_report"])

    first = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose=purpose,
        expected_version=state.version,
        idempotency_key="typed-presend-refusal-first",
    )

    first_reservation = next(
        item
        for item in (await fixture.envelopes.get(fixture.investigation_id)).budget.reservations
        if item.operation_key == first.invocation.operation_key
    )
    assert first.invocation.status is ToolInvocationStatus.POLICY_DENIED
    assert first_reservation.status.value == "released"
    fixture.tools["vt.file_report"].result = None

    retry = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose=purpose,
        expected_version=first.working_state_version,
        idempotency_key="typed-presend-refusal-retry",
    )

    assert retry.invocation.id != first.invocation.id
    assert retry.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert fixture.tools["vt.file_report"].calls == [arguments, arguments]


@pytest.mark.asyncio
async def test_unregistered_vt_adapter_cannot_claim_coordinator_refusal_proof() -> None:
    fixture, state, service = await _fixture()
    arguments = {"query": "untrusted adapter proof"}
    fixture.tools["vt.file_report"].result = ToolResult(
        status=ToolInvocationStatus.POLICY_DENIED,
        error_code="virustotal_external_effect_refused_source_references_mismatch",
        framework_pre_submission_proof=(
            FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
        ),
    )

    first = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose="An untrusted adapter cannot prove non-submission.",
        expected_version=state.version,
        idempotency_key="untrusted-vt-proof-first",
    )

    assert first.invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments=arguments,
            purpose="An untrusted adapter cannot prove non-submission.",
            expected_version=first.working_state_version,
            idempotency_key="untrusted-vt-proof-retry",
        )

    assert error.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    assert fixture.tools["vt.file_report"].calls == [arguments]


class _IdentityProvider:
    async def current(self) -> Identity:
        return Identity(
            actor_id="operator:client-test",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


class _Uow:
    def __init__(self, fixture: _Fixture) -> None:
        self.analyst_investigations = fixture.investigations
        self.investigation_states = fixture.states
        self.investigation_envelopes = fixture.envelopes
        self.analyst_decisions = fixture.analyst_decisions
        self.tool_invocations = fixture.invocations
        self.evidence_observations = fixture.observations
        self.analyst_opinions = fixture.opinions

        class _Jobs:
            async def list_for_aggregate(self, *_args, **_kwargs):
                return ()

        class _ModelConversationTurns:
            async def get_by_idempotency_key(self, *_args, **_kwargs):
                return None

            async def get(self, *_args, **_kwargs):
                return None

        self.jobs = _Jobs()
        self.model_conversation_turns = _ModelConversationTurns()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _CompositionFactory:
    def __init__(
        self,
        invocation_service: OperatorToolInvocationService,
        *,
        fixture: _Fixture | None = None,
        artifact_store: object | None = None,
    ) -> None:
        self.operator_tool_invocations = invocation_service
        self.fixture = fixture
        self.artifact_store = artifact_store
        self.settings = SimpleNamespace(
            operator_advisor_checkpoint_tool_threshold=12,
        )

    def build(
        self,
        _uow: _Uow,
        _investigation_id: object,
        *,
        operator_mode: bool = False,
    ) -> SimpleNamespace:
        assert operator_mode is True
        composition = SimpleNamespace(operator_tool_invocations=self.operator_tool_invocations)
        if self.fixture is not None:
            composition.tool_dependencies = SimpleNamespace(
                article_snapshots=self.fixture.context_builder.article_snapshots,
                artifact_store=self.artifact_store,
            )
            composition.registry = self.fixture.registry
            composition.available_capabilities = self.fixture.available_capabilities
        return composition


@pytest.mark.asyncio
async def test_operator_http_routes_expose_bounded_context_and_idempotent_invocation() -> None:
    fixture, state, service = await _fixture()
    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service, fixture=fixture)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        context = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context",
            params={
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )
        summary = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context",
            params={
                "view": "summary",
                "since_version": state.version,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )
        summary_payload = summary.json()
        evidence_pointer = summary_payload["pointers"]["section_uri_template"].format(
            section="evidence_index",
            cursor=summary_payload["pointers"]["section_keyset_cursors"]["evidence_index"],
            limit=summary_payload["pointers"]["section_limit"],
            version=state.version,
            sha256=state.sha256,
        )
        section = await client.get(
            evidence_pointer,
            params={
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )
        tool_section = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context/section/available_tools",
            params={
                "offset": 0,
                "limit": 20,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )
        finding_versions_page = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context/section/finding_versions",
            params={
                "offset": 0,
                "limit": 20,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )
        stale_summary = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context",
            params={
                "view": "summary",
                "expected_version": state.version,
                "expected_sha256": "f" * 64,
            },
        )
        payload = {
            "schema_version": "1",
            "arguments": {"query": "sample hash"},
            "purpose": "verify report",
            "expected_version": state.version,
        }
        stale_tool = await client.post(
            f"/api/investigations/{fixture.investigation_id}/operator/tools/vt.file_report",
            json={**payload, "expected_version": state.version + 1},
            headers={"Idempotency-Key": "stale-http-key"},
        )
        first = await client.post(
            f"/api/investigations/{fixture.investigation_id}/operator/tools/vt.file_report",
            json=payload,
            headers={"Idempotency-Key": "http-key"},
        )
        replay = await client.post(
            f"/api/investigations/{fixture.investigation_id}/operator/tools/vt.file_report",
            json=payload,
            headers={"Idempotency-Key": "http-key"},
        )
        stale_page = await client.get(evidence_pointer)

    assert context.status_code == 200, context.text
    assert context.json()["working_state_version"] == state.version
    assert "max_context_bytes" not in context.json()
    assert "truncation_counts" in context.json()
    assert "truncation_pointers" in context.json()
    assert "context" in context.json()
    assert summary.status_code == 200, summary.text
    assert summary_payload["schema"] == "investigation-operator-context-summary.v1"
    assert "context" not in summary_payload
    context_payload = context.json()
    semantic_context_payload = json.loads(json.dumps(context_payload["context"]))
    semantic_context_payload["investigation"].pop("pagination_manifest", None)
    expected_context_content = canonical_json_bytes(
        {
            "schema": "operator-context-content.v1",
            "context": semantic_context_payload,
            "current_opinion": context_payload["current_opinion"],
        }
    )
    assert summary_payload["context_sha256"] == hashlib.sha256(expected_context_content).hexdigest()
    assert summary_payload["working_state_sha256"] == state.sha256
    assert summary_payload["delta_since_version"]["from_version"] == state.version
    evidence_cursor = summary_payload["pointers"]["section_cursors"]["evidence_index"]
    assert evidence_cursor == 0
    assert summary_payload["pointers"]["section_keyset_cursors"]["evidence_index"] == ""
    legacy_summary_payload = json.loads(json.dumps(summary_payload))
    legacy_summary_payload.pop("pagination_manifest", None)
    legacy_summary_payload["pointers"].pop("section_keyset_cursors", None)
    legacy_summary = InvestigationOperatorContextSummaryV1.model_validate_json(
        json.dumps(legacy_summary_payload)
    )
    assert legacy_summary.pointers.section_keyset_cursors == {}
    assert (
        "/operator/context/section/{section}" in summary_payload["pointers"]["section_uri_template"]
    )
    assert summary_payload["pointers"]["section_limit"] == 20
    assert len(summary.content) < 16_384
    assert section.status_code == 200, section.text
    assert section.json()["section"] == "evidence_index"
    assert section.json()["offset"] == 0
    assert section.json()["working_state_version"] == state.version
    assert section.json()["working_state_sha256"] == state.sha256
    assert tool_section.status_code == 200, tool_section.text
    assert tool_section.json()["schema"] == "investigation-operator-context-item-page.v1"
    assert tool_section.json()["section"] == "available_tools"
    assert tool_section.json()["items"] == context.json()["context"]["available_tools"]
    tool_section_payload = tool_section.json()
    assert verify_page_sha256(
        claimed_sha256=tool_section_payload["page_sha256"],
        section="available_tools",
        investigation_id=fixture.investigation_id,
        items=tool_section_payload["items"],
        previous_page_sha256="0" * 64,
        context_sha256=tool_section_payload["context_sha256"],
        cursor=tool_section_payload["cursor"],
        pivot=tool_section_payload["pivot"],
        descending=tool_section_payload["descending"],
        limit=tool_section_payload["limit"],
        total=tool_section_payload["total"],
        has_more=tool_section_payload["has_more"],
        omission_manifest=tool_section_payload["omission_manifest"],
        next_cursor=tool_section_payload["next_cursor"],
    )
    assert finding_versions_page.status_code == 200, finding_versions_page.text
    assert finding_versions_page.json()["schema"] == "investigation-operator-context-item-page.v1"
    assert finding_versions_page.json()["section"] == "finding_versions"
    assert finding_versions_page.json()["untrusted_data"] is True
    assert stale_summary.status_code == 409
    assert stale_summary.json()["detail"]["code"] == "stale_working_state"
    assert stale_tool.status_code == 409
    assert stale_tool.json()["detail"]["code"] == "stale_working_state"
    assert stale_page.status_code == 409
    assert stale_page.json()["detail"]["code"] == "stale_working_state"
    assert first.status_code == replay.status_code == 200
    assert first.json()["invocation"]["status"] == "succeeded"
    assert replay.json()["idempotent_replay"] is True
    saved_state = await fixture.states.get(fixture.investigation_id)
    assert saved_state is not None
    assert saved_state.version == state.version + 1
    assert "sample hash" not in first.text
    assert fixture.tools["vt.file_report"].calls == [{"query": "sample hash"}]


@pytest.mark.asyncio
async def test_http_context_cursor_mutation_returns_stale_409() -> None:
    fixture, state, service = await _fixture()
    for index in range(2):
        await fixture.observations.append_if_absent(
            EvidenceObservation(
                investigation_id=fixture.investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=uuid4(),
                sha256=f"{index + 1:064x}",
                source="test.source",
                source_version="1",
                observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                object_type="fixture",
                object_id=str(index),
            )
        )
    first_page = await service.get_context_section(
        "evidence_index",
        limit=1,
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    assert first_page.next_cursor is not None
    await fixture.observations.append_if_absent(
        EvidenceObservation(
            investigation_id=fixture.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256=f"{3:064x}",
            source="test.source",
            source_version="1",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=2),
            object_type="fixture",
            object_id="2",
        )
    )

    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service, fixture=fixture)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        stale_page = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context/section/evidence_index",
            params={
                "cursor": first_page.next_cursor,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )

    assert stale_page.status_code == 409
    assert stale_page.json()["detail"]["code"] == "operator_context_cursor_stale"


@pytest.mark.asyncio
async def test_http_legacy_offset_after_content_change_is_rejected() -> None:
    fixture, state, service = await _fixture()
    base_time = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(2):
        await fixture.observations.append_if_absent(
            EvidenceObservation(
                investigation_id=fixture.investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=uuid4(),
                sha256=f"{index + 1:064x}",
                source="test.source",
                source_version="1",
                observed_at=base_time + timedelta(seconds=index),
                object_type="fixture",
                object_id=str(index),
            )
        )
    first_page = await service.get_context_section(
        "evidence_index",
        limit=1,
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    assert first_page.next_offset == 1
    assert first_page.next_cursor is not None

    await fixture.observations.append_if_absent(
        EvidenceObservation(
            investigation_id=fixture.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256=f"{3:064x}",
            source="test.source",
            source_version="1",
            observed_at=base_time - timedelta(seconds=1),
            object_type="fixture",
            object_id="inserted-before-offset",
        )
    )

    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service, fixture=fixture)
    section_url = (
        f"/api/investigations/{fixture.investigation_id}/operator/context/section/evidence_index"
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        offset_response = await client.get(
            section_url,
            params={
                "offset": first_page.next_offset,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )
        numeric_cursor_response = await client.get(
            section_url,
            params={
                "cursor": str(first_page.next_offset),
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )

    for response in (offset_response, numeric_cursor_response):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "operator_context_offset_cursor_unsupported"
        assert "items" not in response.json()


@pytest.mark.asyncio
async def test_operator_http_reports_invalid_framework_proof_and_keeps_retry_blocked() -> None:
    fixture, state, service = await _fixture()
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    refused_budget = InvestigationBudget(
        limits={**envelope.budget.limits, BudgetDimension.VT_UNITS: 0}
    )
    await fixture.envelopes.save_if_version(
        replace(envelope, budget=refused_budget, version=envelope.version + 1),
        expected_version=envelope.version,
    )

    refused = await service.invoke(
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"query": "proof integrity"},
        purpose="verify the persisted refusal proof",
        expected_version=state.version,
        idempotency_key="invalid-proof-first-attempt",
    )
    assert refused.invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert fixture.tools["vt.file_report"].calls == []
    observation = await fixture.observations.get(refused.invocation.response_refs[0])
    assert observation is not None
    stored = fixture.proof_artifacts.blobs[observation.blob_id]
    fixture.proof_artifacts.blobs[observation.blob_id] = bytes((stored[0] ^ 1,)) + stored[1:]

    latest_envelope = await fixture.envelopes.get(fixture.investigation_id)
    extended_budget = InvestigationBudget(
        limits={**latest_envelope.budget.limits, BudgetDimension.VT_UNITS: 1}
    )
    for reservation in latest_envelope.budget.reservations:
        extended_budget.restore_reservation(reservation)
    await fixture.envelopes.save_if_version(
        replace(
            latest_envelope,
            budget=extended_budget,
            version=latest_envelope.version + 1,
        ),
        expected_version=latest_envelope.version,
    )

    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service, fixture=fixture)
    latest_state = await fixture.states.get(fixture.investigation_id)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/investigations/{fixture.investigation_id}/operator/tools/vt.file_report",
            json={
                "schema_version": "1",
                "arguments": {"query": "proof integrity"},
                "purpose": "verify the persisted refusal proof",
                "expected_version": latest_state.version,
            },
            headers={"Idempotency-Key": "invalid-proof-retry"},
        )

    detail = response.json()["detail"]
    assert response.status_code == 409
    assert detail["code"] == "ambiguous_tool_invocation_requires_reconciliation"
    assert detail["details"] == {
        "existing_invocation_id": str(refused.invocation.id),
        "framework_proof_rejection": "framework_proof_blob_hash_mismatch",
    }
    still_blocked = await fixture.invocations.get_by_operation_key(refused.invocation.operation_key)
    assert still_blocked is not None
    assert still_blocked.status is ToolInvocationStatus.NEEDS_REVIEW
    assert fixture.tools["vt.file_report"].calls == []


@pytest.mark.asyncio
async def test_context_section_keyset_cursor_is_opaque_and_rejects_unbound_offset() -> None:
    fixture, state, service = await _fixture()
    for index in range(25):
        await fixture.observations.append_if_absent(
            EvidenceObservation(
                investigation_id=fixture.investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=uuid4(),
                sha256=f"{index + 1:064x}",
                source="test.source",
                source_version="1",
                observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                object_type="fixture",
                object_id=str(index),
            )
        )

    first = await service.get_context_section(
        "evidence_index",
        limit=20,
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    assert first.next_cursor is not None
    assert not first.next_cursor.isdigit()
    second = await service.get_context_section(
        "evidence_index",
        limit=20,
        expected_version=state.version,
        expected_sha256=state.sha256,
        cursor=first.next_cursor,
    )
    with pytest.raises(OperatorToolInvocationError) as unbound_offset:
        await service.get_context_section(
            "evidence_index",
            offset=20,
            limit=20,
            expected_version=state.version,
            expected_sha256=state.sha256,
        )
    assert unbound_offset.value.code == "operator_context_offset_cursor_unsupported"
    chained = decode_keyset_cursor(
        first.next_cursor,
        section="evidence_index",
        investigation_id=fixture.investigation_id,
        descending=False,
    )

    assert first.total == second.total == 25
    assert len(first.items) == 20
    assert len(second.items) == 5
    assert first.omission_manifest[0].omission_reasons == {
        ContextOmissionReason.PAGINATION: first.total - len(first.items)
    }
    assert ContextOmissionReason.BUDGET not in first.omission_manifest[0].omission_reasons
    full_observations = await fixture.observations.list_for_investigation(fixture.investigation_id)
    expected_evidence_ids = tuple(
        str(item.id)
        for item in sorted(full_observations, key=lambda item: (item.observed_at, item.id.int))
    )
    assert tuple(item["evidence_id"] for item in first.items + second.items) == (
        expected_evidence_ids
    )
    assert tuple(item["evidence_id"] for item in second.items) == expected_evidence_ids[20:]
    assert set(item["evidence_id"] for item in first.items).isdisjoint(
        item["evidence_id"] for item in second.items
    )
    assert chained is not None
    assert verify_page_sha256(
        claimed_sha256=second.page_sha256,
        section="evidence_index",
        investigation_id=fixture.investigation_id,
        items=second.items,
        previous_page_sha256=chained["previous_page_sha256"],
        context_sha256=second.context_sha256,
        cursor=second.cursor,
        pivot=second.pivot,
        descending=second.descending,
        limit=second.limit,
        total=second.total,
        has_more=second.has_more,
        omission_manifest=second.omission_manifest,
        next_cursor=second.next_cursor,
    )
    assert second.omission_manifest[0].cursor_authentication == "unauthenticated_position"

    forged_position = encode_keyset_cursor(
        section="evidence_index",
        investigation_id=fixture.investigation_id,
        sort_key=(datetime.fromisoformat(chained["sort_key"]) + timedelta(seconds=2)).isoformat(),
        item_id=UUID(chained["item_id"]),
        previous_page_sha256=chained["previous_page_sha256"],
        descending=False,
        context_sha256=first.context_sha256,
    )
    forged_page = await service.get_context_section(
        "evidence_index",
        limit=20,
        expected_version=state.version,
        expected_sha256=state.sha256,
        cursor=forged_position,
    )
    assert forged_page.section == second.section
    assert forged_page.total == second.total
    assert forged_page.page_sha256 != second.page_sha256
    assert verify_page_sha256(
        claimed_sha256=forged_page.page_sha256,
        section="evidence_index",
        investigation_id=fixture.investigation_id,
        items=forged_page.items,
        previous_page_sha256=chained["previous_page_sha256"],
        context_sha256=forged_page.context_sha256,
        cursor=forged_page.cursor,
        pivot=forged_page.pivot,
        descending=forged_page.descending,
        limit=forged_page.limit,
        total=forged_page.total,
        has_more=forged_page.has_more,
        omission_manifest=forged_page.omission_manifest,
        next_cursor=forged_page.next_cursor,
    )
    assert not verify_page_sha256(
        claimed_sha256=forged_page.page_sha256,
        section="evidence_index",
        investigation_id=fixture.investigation_id,
        items=forged_page.items,
        previous_page_sha256=chained["previous_page_sha256"],
        context_sha256=forged_page.context_sha256,
        cursor=first.next_cursor,
        pivot=forged_page.pivot,
        descending=forged_page.descending,
        limit=forged_page.limit,
        total=forged_page.total,
        has_more=forged_page.has_more,
        omission_manifest=forged_page.omission_manifest,
        next_cursor=forged_page.next_cursor,
    )

    for bad_scope, expected_code in (
        (
            encode_keyset_cursor(
                section="findings",
                investigation_id=fixture.investigation_id,
                sort_key=chained["sort_key"],
                item_id=UUID(chained["item_id"]),
                previous_page_sha256=chained["previous_page_sha256"],
                descending=True,
                context_sha256=first.context_sha256,
            ),
            "operator_context_cursor_invalid",
        ),
        (
            encode_keyset_cursor(
                section="evidence_index",
                investigation_id=uuid4(),
                sort_key=chained["sort_key"],
                item_id=UUID(chained["item_id"]),
                previous_page_sha256=chained["previous_page_sha256"],
                descending=False,
                context_sha256=first.context_sha256,
            ),
            "operator_context_cursor_invalid",
        ),
        (
            encode_keyset_cursor(
                section="evidence_index",
                investigation_id=fixture.investigation_id,
                sort_key=chained["sort_key"],
                item_id=UUID(chained["item_id"]),
                previous_page_sha256=chained["previous_page_sha256"],
                descending=False,
                context_sha256="f" * 64,
            ),
            "operator_context_cursor_stale",
        ),
    ):
        with pytest.raises(OperatorToolInvocationError) as bad_scope_error:
            await service.get_context_section(
                "evidence_index",
                expected_version=state.version,
                expected_sha256=state.sha256,
                cursor=bad_scope,
            )
        assert bad_scope_error.value.code == expected_code
    with pytest.raises(OperatorToolInvocationError) as invalid:
        await service.get_context_section(
            "evidence_index",
            expected_version=state.version,
            expected_sha256=state.sha256,
            cursor="not-a-keyset-cursor",
        )
    assert invalid.value.code == "operator_context_cursor_invalid"


@pytest.mark.asyncio
async def test_context_cursor_binds_canonical_content_and_rejects_stale_continuations() -> None:
    fixture, state, service = await _fixture()
    for index in range(3):
        await fixture.observations.append_if_absent(
            EvidenceObservation(
                investigation_id=fixture.investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=uuid4(),
                sha256=f"{index + 1:064x}",
                source="test.source",
                source_version="1",
                observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                object_type="fixture",
                object_id=str(index),
            )
        )

    first = await service.get_context_section(
        "evidence_index",
        limit=1,
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    different_page_size = await service.get_context_section(
        "evidence_index",
        limit=2,
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    assert first.next_cursor is not None
    assert first.context_sha256 == different_page_size.context_sha256
    assert first.context_sha256 != state.sha256
    operator_context = await service.get_context(
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    current_opinion = operator_context.context.current_opinion
    context = operator_context.context
    investigation_context = context.investigation
    expected_context_fields = context.model_dump(
        mode="json",
        by_alias=True,
        include={
            "schema_version": True,
            "view": True,
            "investigation_id": True,
            "source": True,
            "article": True,
            "investigation": {
                "agenda": True,
                "hypotheses": True,
                "new_evidence": True,
                "evidence_index": True,
                "evidence_index_truncated": True,
                "findings": True,
                "findings_truncated": True,
                "opinion_notes": True,
                "opinion_notes_truncated": True,
                "truncation_counts": True,
                "new_evidence_truncated": True,
                "hypotheses_truncated": True,
                "rule_deltas_truncated": True,
                "previous_actions_truncated": True,
                "long_texts_truncated": True,
                "sample_deltas": True,
                "rule_deltas": True,
                "previous_actions": True,
                "user_requests": True,
                "tool_coverage": True,
                "tool_coverage_truncated": True,
                "latest_tool_results": True,
                "sample_deltas_truncated": True,
                "blocked_capabilities": True,
                "budget_remaining": True,
            },
            "operator_requests": True,
            "indicator_inventory": True,
            "available_pivots": True,
            "available_pivots_truncated": True,
            "delta": True,
            "available_tools": True,
            "unavailable_tools": True,
            "unavailable_tools_truncated": True,
            "advisor_guidance": True,
        },
    )
    canonical_content = canonical_json_bytes(
        {
            "schema": "operator-context-content.v1",
            "context": expected_context_fields,
            "current_opinion": (
                current_opinion.model_dump(mode="json", by_alias=True)
                if current_opinion is not None
                else None
            ),
        }
    )
    assert first.context_sha256 == hashlib.sha256(canonical_content).hexdigest()

    page_proof = ContextOmissionManifestEntryV1(
        section="evidence_index",
        total=1,
        selected=1,
        omitted=0,
        source_bounded=True,
        source_ref="test-page-reference",
        page_sha256="a" * 64,
    )
    proof_context = context.model_copy(
        update={
            "investigation": investigation_context.model_copy(
                update={"pagination_manifest": (page_proof,)}
            )
        }
    )
    changed_page_proof = page_proof.model_copy(update={"page_sha256": "b" * 64})
    changed_proof_context = context.model_copy(
        update={
            "investigation": investigation_context.model_copy(
                update={"pagination_manifest": (changed_page_proof,)}
            )
        }
    )
    assert hashlib.sha256(_canonical_context_content_bytes(proof_context)).hexdigest() == (
        hashlib.sha256(_canonical_context_content_bytes(changed_proof_context)).hexdigest()
    )

    await fixture.observations.append_if_absent(
        EvidenceObservation(
            investigation_id=fixture.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256=f"{4:064x}",
            source="test.source",
            source_version="1",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=4),
            object_type="fixture",
            object_id="4",
        )
    )
    changed_context = await service.get_context_section(
        "evidence_index",
        limit=1,
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    assert changed_context.context_sha256 != first.context_sha256
    with pytest.raises(StaleContextCursorError):
        decode_keyset_cursor(
            first.next_cursor,
            section="evidence_index",
            investigation_id=fixture.investigation_id,
            descending=False,
            context_sha256=changed_context.context_sha256,
        )

    with pytest.raises(OperatorToolInvocationError) as stale:
        await service.get_context_section(
            "evidence_index",
            limit=1,
            expected_version=state.version,
            expected_sha256=state.sha256,
            cursor=first.next_cursor,
        )
    assert stale.value.code == "operator_context_cursor_stale"


@pytest.mark.asyncio
async def test_materialized_offset_cursors_bind_the_canonical_context() -> None:
    fixture, state, service = await _fixture()
    agenda_first = await service.get_context_section(
        "agenda",
        limit=1,
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    assert agenda_first.next_cursor is not None
    agenda_continuation = await service.get_context_section(
        "agenda",
        limit=1,
        expected_version=state.version,
        expected_sha256=state.sha256,
        cursor=agenda_first.next_cursor,
    )
    assert agenda_continuation.offset == 1
    assert agenda_continuation.context_sha256 == agenda_first.context_sha256

    original_read_context_section = fixture.context_builder.read_context_section

    async def read_two_article_claims(**kwargs):
        if kwargs["section"] == "article_claims":
            claims = (
                {"id": "C1", "statement": "First claim", "source_refs": []},
                {"id": "C2", "statement": "Second claim", "source_refs": []},
            )
            offset = kwargs["offset"]
            return len(claims), claims[offset : offset + kwargs["limit"]]
        return await original_read_context_section(**kwargs)

    fixture.context_builder.read_context_section = read_two_article_claims
    article_claims_first = await service.get_context_section(
        "article_claims",
        limit=1,
        expected_version=state.version,
        expected_sha256=state.sha256,
    )
    assert article_claims_first.next_cursor is not None

    await fixture.observations.append_if_absent(
        EvidenceObservation(
            investigation_id=fixture.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256=f"{5:064x}",
            source="test.source",
            source_version="1",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            object_type="fixture",
            object_id="content-change",
        )
    )

    for section, cursor in (
        ("agenda", agenda_first.next_cursor),
        ("article_claims", article_claims_first.next_cursor),
    ):
        with pytest.raises(OperatorToolInvocationError) as stale:
            await service.get_context_section(
                section,
                limit=1,
                expected_version=state.version,
                expected_sha256=state.sha256,
                cursor=cursor,
            )
        assert stale.value.code == "operator_context_cursor_stale"
        assert stale.value.status_code == 409


@pytest.mark.asyncio
async def test_operator_context_larger_than_256kib_is_returned_without_a_size_refusal() -> None:
    fixture, state, service = await _fixture()
    build_context = fixture.context_builder.build

    async def oversized(**kwargs):
        context = await build_context(**kwargs)
        large_tool = context.available_tools[0].model_copy(
            update={
                "arguments_schema": {
                    "type": "object",
                    "description": "x" * 300_000,
                    "properties": {},
                }
            }
        )
        return context.model_copy(update={"available_tools": (large_tool,)})

    fixture.context_builder.build = oversized
    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context",
            params={"expected_version": state.version},
        )

    assert response.status_code == 200, response.text
    assert response.json()["context_bytes"] > 262_144
    assert "max_context_bytes" not in response.json()


@pytest.mark.asyncio
async def test_operator_context_validation_error_is_logged_without_input_values(caplog) -> None:
    fixture, state, service = await _fixture()
    invalid_context = ValidationError.from_exception_data(
        "InvalidContext",
        [
            {
                "type": "string_type",
                "loc": ("source", "synthesis_sha256"),
                "input": "private-secret-value",
            }
        ],
    )

    async def invalid(**_kwargs):
        raise invalid_context

    fixture.context_builder.build = invalid
    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context",
            params={"expected_version": state.version},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "operator_context_invalid"
    assert "operator_context_validation_failed" in caplog.text
    assert "private-secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_operator_http_route_rejects_invalid_body_before_dispatch() -> None:
    fixture, _state, service = await _fixture()
    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/investigations/{fixture.investigation_id}/operator/tools/vt.file_report",
            json={"schema_version": "1", "arguments": {}, "purpose": "x", "extra": True},
            headers={"Idempotency-Key": "invalid-body"},
        )

    assert response.status_code == 422
    assert fixture.tools["vt.file_report"].calls == []

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from cti_app.api.investigation import router
from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_SCHEMA_VERSION,
    InvestigationAdvisorTool,
    register_investigation_advisor_tool,
)
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
    OperatorToolInvocationService,
)
from cti_app.application.research_context_builder import InvestigationContextTooLarge
from cti_app.config import Settings
from cti_app.domain.investigation import (
    BudgetDimension,
    InvestigationBudget,
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from tests.test_investigation_service import _Fixture


def _service(fixture: _Fixture) -> OperatorToolInvocationService:
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
    replay = await service.invoke(**request)

    assert first.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert first.output["untrusted_data"] is True
    assert first.output["kind"] == "vt_file_report"
    assert first.output["data"]["result"]["summary"] == (
        "vt.file_report observed the requested relation."
    )
    assert replay.idempotent_replay is True
    assert replay.invocation.id == first.invocation.id
    assert fixture.tools["vt.file_report"].calls == [{"query": "sample hash"}]
    assert first.invocation.operation_key in fixture.committed_running_keys
    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(**{**request, "purpose": "different payload"})
    assert error.value.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_operator_dispatcher_wires_minimal_advisor_request_to_strict_bridge_transport():
    fixture, state, _ = await _fixture()
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
            }
            assert kwargs["external_llm_allowed"] is True
            assert len(kwargs["prompt"].encode()) <= 12 * 1024
            self.calls.append(kwargs)
            return TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=json.dumps(
                    {
                        "summary": "Comparer les faits mesurés avant un nouveau pivot.",
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
            operator_advisor_min_consultation_interval_seconds=0,
        ),
        investigations=fixture.investigations,
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
        arguments={"consultation_type": "plan_investigation"},
        purpose="Orienter le prochain pivot a partir de l'etat mesure.",
        expected_version=state.version,
        idempotency_key="operator-advisor-minimal-e2e",
    )

    assert result.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert len(transport.calls) == 1
    assert "UNTRUSTED_ADVISORY_INPUT" in transport.calls[0]["prompt"]
    refs = [await fixture.observations.get(ref) for ref in result.invocation.response_refs]
    assert any(item is not None and item.source == "advisor.consult" for item in refs)
    assert result.output["data"]["result"]["summary"].startswith("Comparer")


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


class _IdentityProvider:
    async def current(self) -> Identity:
        return Identity(
            actor_id="operator:client-test",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


class _Uow:
    def __init__(self, fixture: _Fixture) -> None:
        self.analyst_investigations = fixture.investigations

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _CompositionFactory:
    def __init__(self, invocation_service: OperatorToolInvocationService) -> None:
        self.operator_tool_invocations = invocation_service

    def build(
        self,
        _uow: _Uow,
        _investigation_id: object,
        *,
        operator_mode: bool = False,
    ) -> SimpleNamespace:
        assert operator_mode is True
        return SimpleNamespace(operator_tool_invocations=self.operator_tool_invocations)


@pytest.mark.asyncio
async def test_operator_http_routes_expose_bounded_context_and_idempotent_invocation() -> None:
    fixture, state, service = await _fixture()
    app = FastAPI()
    app.include_router(router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        context = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context",
            params={"expected_version": state.version},
        )
        section = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context/section/evidence_index",
            params={"offset": 0, "limit": 20, "expected_version": state.version},
        )
        payload = {
            "schema_version": "1",
            "arguments": {"query": "sample hash"},
            "purpose": "verify report",
            "expected_version": state.version,
        }
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

    assert context.status_code == 200, context.text
    assert context.json()["working_state_version"] == state.version
    assert context.json()["max_context_bytes"] == 262144
    assert "truncation_counts" in context.json()
    assert "truncation_pointers" in context.json()
    assert section.status_code == 200, section.text
    assert section.json()["section"] == "evidence_index"
    assert section.json()["offset"] == 0
    assert first.status_code == replay.status_code == 200
    assert first.json()["invocation"]["status"] == "succeeded"
    assert replay.json()["idempotent_replay"] is True
    assert "sample hash" not in first.text
    assert fixture.tools["vt.file_report"].calls == [{"query": "sample hash"}]


@pytest.mark.asyncio
async def test_operator_context_too_large_returns_dominant_sections(caplog) -> None:
    fixture, state, service = await _fixture()

    async def oversized(**_kwargs):
        raise InvestigationContextTooLarge(
            300_000,
            dominant_sections=("available_tools:270000", "article:30000"),
        )

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

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "operator_context_too_large"
    assert response.json()["detail"]["details"]["dominant_sections"] == [
        "available_tools:270000",
        "article:30000",
    ]
    assert "operator_context_too_large" in caplog.text
    assert "available_tools:270000" in caplog.text


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

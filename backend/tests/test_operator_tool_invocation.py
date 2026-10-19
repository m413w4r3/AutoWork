from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from cti_app.api.investigation import router
from cti_app.application.advisor_checkpoint import (
    advisor_checkpoint_summary,
    format_advisor_trace_purpose,
)
from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_RESPONSE_SCHEMA,
    ADVISOR_SCHEMA_VERSION,
    InvestigationAdvisorTool,
    register_investigation_advisor_tool,
)
from cti_app.application.investigation_tools import ToolResult
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
    OperatorToolInvocationService,
)
from cti_app.application.research_context_builder import InvestigationContextTooLarge
from cti_app.config import Settings
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
)
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
            input_json = (
                kwargs["prompt"]
                .split("<UNTRUSTED_ADVISORY_INPUT>\n", 1)[1]
                .split("\n</UNTRUSTED_ADVISORY_INPUT>", 1)[0]
            )
            article_ref = json.loads(input_json)["article_source_ref"]
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
    state = await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
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

    class SummaryReader:
        async def summary(self, observation):
            raw = artifacts.values.get(observation.blob_id)
            return json.loads(raw) if raw is not None else None

    summaries = SummaryReader()

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
                                "args_hint": {"query": "a" * 64},
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
    assert advice.invocation.status is ToolInvocationStatus.SUCCEEDED
    assert persisted_advice
    assert advice.invocation.consumed_budget[BudgetDimension.BRIDGE_TURNS] == 1
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
            cursor=summary_payload["pointers"]["section_cursors"]["evidence_index"],
            limit=summary_payload["pointers"]["section_limit"],
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
    assert "context" in context.json()
    assert summary.status_code == 200, summary.text
    assert summary_payload["schema"] == "investigation-operator-context-summary.v1"
    assert "context" not in summary_payload
    assert summary_payload["context_sha256"] == context.json()["context_sha256"]
    assert summary_payload["working_state_sha256"] == state.sha256
    assert summary_payload["delta_since_version"]["from_version"] == state.version
    assert summary_payload["pointers"]["section_cursors"]["evidence_index"] == 0
    assert (
        "/operator/context/section/{section}" in summary_payload["pointers"]["section_uri_template"]
    )
    assert summary_payload["pointers"]["section_limit"] == 20
    assert len(summary.content) < 16_384
    assert section.status_code == 200, section.text
    assert section.json()["section"] == "evidence_index"
    assert section.json()["offset"] == 0
    assert tool_section.status_code == 200, tool_section.text
    assert tool_section.json()["schema"] == "investigation-operator-context-item-page.v1"
    assert tool_section.json()["section"] == "available_tools"
    assert tool_section.json()["items"] == context.json()["context"]["available_tools"]
    assert finding_versions_page.status_code == 200, finding_versions_page.text
    assert finding_versions_page.json()["schema"] == "investigation-operator-context-item-page.v1"
    assert finding_versions_page.json()["section"] == "finding_versions"
    assert finding_versions_page.json()["untrusted_data"] is True
    assert stale_summary.status_code == 409
    assert stale_summary.json()["detail"]["code"] == "stale_working_state"
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

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from pydantic import SecretStr

from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.investigation_contracts import (
    ArticleClaimV1,
    ArticleContextV1,
    parse_agent_turn_decision,
)
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
)
from cti_app.application.investigation_shodan_tools import (
    SCHEMA_DRAFT,
    register_shodan_tools,
    shodan_tool_capabilities,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolEffect,
    ToolExecutionContext,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import ArticleSnapshot
from cti_app.application.shodan import (
    ShodanBudgetEstimate,
    ShodanCapabilities,
    ShodanCapabilityDisabledError,
    ShodanHttpError,
    ShodanNotFoundError,
    ShodanQuotaError,
    ShodanTransportError,
)
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetReservationStatus,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    TLPLevel,
    ToolInvocationStatus,
    tool_operation_key,
)
from cti_app.domain.investigation_state import StopReason
from cti_app.infrastructure.shodan import ShodanHttpAdapter
from tests.shodan_support import CAPTURED_AT, HOST_RESPONSE, FakeShodanPort
from tests.test_investigation_service import (
    _ArticlePort,
    _decision,
    _finding_id,
    _Fixture,
    _operation_and_observation_id,
    _proposal,
)

_EVIDENCE_NAMESPACE = UUID("47bfec4f-9070-42d0-aef5-0eb84c8b57cd")
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")
_SHODAN_IP = "203.0.113.10"


class _BlobStore:
    def __init__(self) -> None:
        self.blobs: dict[UUID, bytes] = {}

    async def put_canonical_json(self, payload, *, bucket):
        encoded = ProductionArtifactStore.canonical_json_bytes(payload)
        sha256 = hashlib.sha256(encoded).hexdigest()
        blob_id = uuid5(UUID("a73cde63-2629-40ba-9b2f-9575a7d8195e"), sha256)
        self.blobs[blob_id] = encoded
        return blob_id, sha256

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        sha256 = hashlib.sha256(content).hexdigest()
        blob_id = uuid5(UUID("a73cde63-2629-40ba-9b2f-9575a7d8195e"), f"{bucket}:{sha256}")
        self.blobs[blob_id] = content
        return blob_id


class _RawArchive:
    def __init__(self, store: _BlobStore) -> None:
        self.store = store
        self.calls: list[tuple[bytes, str, str]] = []

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        self.calls.append((content, bucket, mime_type))
        return await self.store.put_bytes(content, bucket=bucket, mime_type=mime_type)


class _Rig:
    def __init__(
        self,
        *,
        shodan: FakeShodanPort | None = None,
        capabilities: ShodanCapabilities | None = None,
        allowed_providers: tuple[str, ...] = ("shodan",),
        shodan_budget: int = 10,
        article_snapshot: ArticleSnapshot | None = None,
        raw_archive: _RawArchive | None = None,
        max_response_bytes: int | None = None,
    ) -> None:
        self.investigation_id = uuid4()
        self.budget = InvestigationBudget(limits={BudgetDimension.SHODAN_CREDITS: shodan_budget})
        self.envelope = InvestigationEnvelope(
            investigation_id=self.investigation_id,
            allowed_providers=allowed_providers,
            budget=self.budget,
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            approval_decision_id=uuid4(),
            approval_actor_id="analyst-test",
        )
        self.context = None
        configured = capabilities or ShodanCapabilities(
            host=True, search=True, domain=True, count=True
        )
        self.capabilities = shodan_tool_capabilities(configured)
        self.observations = InMemoryEvidenceObservationRepository()
        self.findings = InMemoryFindingRepository()
        self.store = _BlobStore()
        self.recorder = ProductionEvidenceRecorder(self.store, self.observations)  # type: ignore[arg-type]
        self.raw_archive = raw_archive
        self.port = shodan
        self.snapshot = article_snapshot or _article_snapshot(self.investigation_id)
        self.registry = InMemoryToolRegistry()
        register_shodan_tools(
            self.registry,
            shodan=shodan,
            recorder=self.recorder,
            article_snapshots=self.snapshot,
            observations=self.observations,
            findings=self.findings,
            capabilities=capabilities,
            budget_estimate=ShodanBudgetEstimate(),
            raw_archive=raw_archive,
            max_response_bytes=max_response_bytes,
        )
        self.dispatcher = RegistryToolDispatcher(self.registry)

    def _context_for(
        self, key: str, *, capabilities: frozenset[str] | None = None
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            investigation_id=self.investigation_id,
            envelope=self.envelope,
            budget=self.budget,
            idempotency_key=key,
            available_capabilities=capabilities or self.capabilities,
        )

    async def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, object],
        *,
        context: ToolExecutionContext | None = None,
        purpose: str = "test passive Shodan query",
        operation_key: str | None = None,
    ):
        key = operation_key or tool_operation_key(
            investigation_id=self.investigation_id,
            tool_name=tool_name,
            schema_version="1",
            normalized_parameters=arguments,
            purpose=purpose,
        )
        active_context = context or ToolExecutionContext(
            investigation_id=self.investigation_id,
            envelope=self.envelope,
            budget=self.budget,
            idempotency_key=key,
            available_capabilities=self.capabilities,
        )
        return await self.dispatcher.dispatch(
            active_context,
            tool_name=tool_name,
            schema_version="1",
            arguments=arguments,
            purpose=purpose,
            operation_key=key,
        )


def _article_snapshot(investigation_id: UUID) -> ArticleSnapshot:
    return ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        article=ArticleContextV1(
            short_summary="Article infrastructure snapshot.",
            claims=(
                ArticleClaimV1(
                    id="C1",
                    statement=f"IP {_SHODAN_IP} is a C2 for family F.",
                    source_refs=("source-1",),
                ),
            ),
            documented_samples=(),
            known_chain=({"kind": "documented_ioc", "artifact_type": "ip", "value": _SHODAN_IP},),
            open_questions=(),
        ),
    )


async def _add_vt_relationship(rig: _Rig, *, investigation_id: UUID | None = None) -> UUID:
    observation_id = uuid4()
    observation = EvidenceObservation(
        id=observation_id,
        investigation_id=investigation_id or rig.investigation_id,
        kind=EvidenceKind.VT_RELATIONSHIP,
        blob_id=uuid4(),
        sha256="b" * 64,
        source="virustotal",
        source_version="v1",
        observed_at=datetime(2026, 10, 12, tzinfo=UTC),
        object_type="ip",
        object_id=_SHODAN_IP,
    )
    await rig.observations.append_if_absent(observation)
    return observation_id


@pytest.mark.asyncio
async def test_registers_2020_12_schemas_and_rejects_invalid_shodan_arguments() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port)
    definitions = {
        name: rig.registry.get(name, "1")[0]
        for name in ("shodan.host", "shodan.search", "shodan.domain", "shodan.count")
    }
    assert all(
        definition.input_schema["$schema"] == SCHEMA_DRAFT for definition in definitions.values()
    )
    assert all(
        definition.input_schema["additionalProperties"] is False
        for definition in definitions.values()
    )
    assert definitions["shodan.search"].input_schema["required"] == (
        "query",
        "justification",
        "evidence_refs",
    )
    assert rig.capabilities == frozenset(
        {"shodan.host", "shodan.search", "shodan.domain", "shodan.count"}
    )

    bad_host = await rig.dispatch("shodan.host", {"ip": "not-an-ip"})
    bad_search = await rig.dispatch(
        "shodan.search",
        {"query": "scan:1.2.3.4", "justification": "test", "evidence_refs": []},
    )
    assert bad_host.status is ToolInvocationStatus.FAILED
    assert bad_search.status is ToolInvocationStatus.FAILED
    assert port.calls == []


def test_compact_shodan_count_justification_keeps_commas_using_real_schema() -> None:
    rig = _Rig(shodan=FakeShodanPort())
    registered = rig.registry.get("shodan.count", "1")
    assert registered is not None
    definition, _adapter = registered
    wire = """@@DECISION@@
GOAL: Count the listed hosts.
SUMMARY: Preserve the full justification as a string.
NEXT: continue
@@END@@
@@TOOL@@
NAME: shodan.count
VERSION: 1
PURPOSE: Count the listed hosts.
ARG query: port:443
ARG justification: Check the first marker, compare the passive index, then report the scope.
ARG evidence_refs: []
@@END@@"""

    decision = parse_agent_turn_decision(
        wire,
        tool_schemas={(definition.name, definition.schema_version): definition.input_schema},
    )

    assert decision.proposed_tool_calls[0].arguments["justification"] == (
        "Check the first marker, compare the passive index, then report the scope."
    )


@pytest.mark.asyncio
async def test_invalid_shodan_count_arguments_are_definitive_local_failures() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port)
    invalid_arguments = (
        {
            "query": "port:443",
            "justification": ["SHODAN_API_KEY=synthetic-secret", "second"],
            "evidence_refs": [],
        },
        {"query": "port:443", "evidence_refs": []},
        {
            "query": "port:443",
            "justification": "synthetic check",
            "evidence_refs": [],
            "unexpected": "extra",
        },
    )

    for index, arguments in enumerate(invalid_arguments):
        result = await rig.dispatch(
            "shodan.count",
            arguments,
            operation_key=f"invalid-shodan-count-{index}",
        )
        assert result.status is ToolInvocationStatus.FAILED
        assert result.error_code == "invalid_tool_arguments"
        detail = result.output["validation_errors"][0]
        if index == 0:
            assert detail["argument"] == "justification"
            assert detail["expected"] == "string"
            assert detail["received"] == "array"
            assert "synthetic-secret" not in result.message
            assert "synthetic-secret" not in detail["value"]
        elif index == 1:
            assert detail["argument"] == "justification"
            assert detail["received"] == "missing"
        else:
            assert detail["argument"] == "unexpected"
            assert detail["expected"] == "no additional properties"
            assert detail["received"] == "extra property"
    assert port.calls == []


@pytest.mark.asyncio
async def test_invalid_shodan_arguments_release_budget_and_reach_the_next_agent_turn() -> None:
    provider = FakeShodanPort()
    real_registry = _Rig(shodan=provider).registry
    registered = real_registry.get("shodan.count", "1")
    assert registered is not None
    definition, adapter = registered
    invalid_call = {
        "tool_name": "shodan.count",
        "schema_version": "1",
        "arguments": {
            "query": "port:443",
            "justification": ["compare the marker", "record the scope"],
            "evidence_refs": [],
        },
        "purpose": "Check the synthetic query.",
    }
    fixture = _Fixture(
        responses=[
            _decision(
                goal="Validate the Shodan request.",
                next_action="continue",
                tools=(invalid_call,),
            ),
            _decision(goal="Finish after the correction is shown.", next_action="finish"),
        ],
        tools=(
            (
                "shodan.count",
                "shodan",
                ToolEffect.EXTERNAL_READ,
                definition.capability_key,
                dict(definition.cost_model),
            ),
        ),
    )
    fixture.registry.unregister("shodan.count", "1")
    fixture.registry.register(definition, adapter)

    initial = await fixture.start()
    state = await fixture.service.run(initial.investigation_id)

    invocation = (await fixture.invocations.list_for_investigation(fixture.investigation_id))[0]
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    reservation = next(
        item
        for item in envelope.budget.reservations
        if item.operation_key == invocation.operation_key
    )
    assert state.stop_reason is StopReason.AGENT_FINISHED
    assert invocation.status is ToolInvocationStatus.FAILED
    assert invocation.consumed_budget == {}
    assert reservation.status is BudgetReservationStatus.RELEASED
    assert provider.calls == []
    assert len(fixture.bridge_transport.prompts) == 2
    assert "invalid_tool_arguments" in fixture.bridge_transport.prompts[1]
    assert "justification" in fixture.bridge_transport.prompts[1]


@pytest.mark.asyncio
async def test_absent_capability_is_local_unavailable_without_provider_call() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port, capabilities=ShodanCapabilities())

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert port.calls == []


@pytest.mark.asyncio
async def test_missing_shodan_port_is_local_unavailable_without_provider_call() -> None:
    rig = _Rig(shodan=None)

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_adapter_budget_preflight_denies_unallocated_credits_without_call() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port, shodan_budget=0)
    adapter = rig.registry.get("shodan.host", "1")[1]

    result = await adapter.execute({"ip": _SHODAN_IP}, rig._context_for("budget-test"))

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output["reason"] == "credits_not_allocated"
    assert result.suggested_alternatives
    assert port.calls == []


@pytest.mark.asyncio
async def test_quota_error_maps_to_unavailable_with_quota_reason() -> None:
    class QuotaPort(FakeShodanPort):
        async def host(self, ip):
            self.calls.append(("host", (ip,)))
            raise ShodanQuotaError("api_key=provider-secret quota reached")

    port = QuotaPort()
    rig = _Rig(shodan=port)

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["reason"] == "quota"
    assert result.budget_consumed[BudgetDimension.SHODAN_CREDITS] == 0
    assert "provider-secret" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_provider_authorization_refusal_maps_to_final_unavailable(status_code: int) -> None:
    class AuthorizationPort(FakeShodanPort):
        async def host(self, ip):
            self.calls.append(("host", (ip,)))
            raise ShodanHttpError(
                "synthetic provider authorization refusal",
                status_code=status_code,
            )

    port = AuthorizationPort()
    rig = _Rig(shodan=port)

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["reason"] == "provider_authorization_refused"
    assert port.calls == [("host", (_SHODAN_IP,))]


@pytest.mark.asyncio
async def test_authorization_unavailable_details_include_safe_provider_diagnostic() -> None:
    class AuthorizationPort(FakeShodanPort):
        async def host(self, ip):
            self.calls.append(("host", (ip,)))
            raise ShodanCapabilityDisabledError(
                "provider authorization refused",
                code="shodan_upstream_forbidden",
                status_code=403,
                upstream_message="Requires membership or higher to access",
                plan="oss",
                query_credits=0,
            )

    result = await _Rig(shodan=AuthorizationPort()).dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    details = result.output["details"]
    assert details["upstream_message"] == "Requires membership or higher to access"
    assert details["plan"] == "oss"
    assert details["query_credits"] == 0
    assert any("oss" in item for item in details["limitations"])


@pytest.mark.asyncio
async def test_forbidden_http_response_reaches_unavailable_tool_result_safely() -> None:
    api_key = "synthetic-shodan-key"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api-info":
            return httpx.Response(
                200,
                json={"plan": "oss", "query_credits": 0},
                request=request,
            )
        return httpx.Response(
            403,
            json={"error": f"Requires membership; key={api_key}"},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    shodan = ShodanHttpAdapter(
        client=client,
        capabilities=ShodanCapabilities(host=True),
        api_key=SecretStr(api_key),
    )
    try:
        result = await _Rig(shodan=shodan).dispatch("shodan.host", {"ip": _SHODAN_IP})
    finally:
        await client.aclose()

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    details = result.output["details"]
    assert details["upstream_message"] == "Requires membership; key=[REDACTED]"
    assert details["plan"] == "oss"
    assert details["query_credits"] == 0
    assert any("oss" in item for item in details["limitations"])
    assert api_key not in str(result)


@pytest.mark.asyncio
async def test_404_records_negative_observation_and_returns_empty() -> None:
    class NotFoundPort(FakeShodanPort):
        async def host(self, ip):
            self.calls.append(("host", (ip,)))
            raise ShodanNotFoundError("No information available")

    port = NotFoundPort()
    rig = _Rig(shodan=port)

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.EMPTY
    assert len(result.observation_refs) == 1
    observation = await rig.observations.get(result.observation_refs[0])
    assert observation is not None
    assert observation.kind is EvidenceKind.NEGATIVE_RESULT
    assert observation.source == "shodan"
    assert observation.object_id == _SHODAN_IP
    payload = json.loads(rig.store.blobs[observation.blob_id])
    assert payload["details"]["captured_at"]
    assert payload["details"]["coverage_limits"]
    assert payload["details"]["credits_consumed"] == 0


@pytest.mark.asyncio
async def test_search_page_cap_returns_partial_and_records_bounded_observation() -> None:
    class PartialPort(FakeShodanPort):
        async def search(self, query, page=1):
            result = await super().search(query, page)
            return replace(result, total=50, matches=result.matches[:1])

    port = PartialPort()
    rig = _Rig(shodan=port)
    vt_ref = await _add_vt_relationship(rig)
    cited_finding = InvestigationFinding(
        id=uuid4(),
        investigation_id=rig.investigation_id,
        kind=FindingKind.NETWORK_BEHAVIOUR,
        assessment=FindingAssessment.QUALIFIED,
        statement="An earlier observation already names this infrastructure.",
        observation_refs=(vt_ref,),
        claim_refs=("C1",),
    )
    await rig.findings.append_version(cited_finding, expected_previous_id=None)
    args = {
        "query": f"ip:{_SHODAN_IP}",
        "justification": "Vérifier l'IOC réseau de l'article.",
        "evidence_refs": [str(vt_ref), str(cited_finding.id)],
        "page": 1,
    }

    result = await rig.dispatch("shodan.search", args)

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.budget_consumed[BudgetDimension.SHODAN_CREDITS] == 1
    observation = await rig.observations.get(result.observation_refs[0])
    assert observation is not None
    assert observation.kind is EvidenceKind.SHODAN_OBSERVATION
    assert "page" in " ".join(observation.limitations)
    payload = json.loads(rig.store.blobs[observation.blob_id])
    assert payload["summary"]["evidence_refs"] == [str(vt_ref), str(cited_finding.id)]


@pytest.mark.asyncio
async def test_search_rejects_unknown_evidence_refs_before_external_call() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port)
    args = {
        "query": "port:443",
        "justification": "Vérifier un service.",
        "evidence_refs": [str(uuid4())],
    }

    result = await rig.dispatch("shodan.search", args)

    assert result.status is ToolInvocationStatus.FAILED
    assert result.output["reason"] == "invalid_input"
    assert port.calls == []


@pytest.mark.asyncio
async def test_policy_denial_is_local_and_suggests_registered_alternatives() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port, allowed_providers=())
    # The dispatcher only suggests alternatives that are actually registered and available.
    from cti_app.application.investigation_tools import (
        ToolConfidentiality,
        ToolDefinition,
        ToolEffect,
        ToolResult,
    )

    class LocalAdapter:
        async def execute(self, arguments, context):
            return ToolResult(ToolInvocationStatus.SUCCEEDED)

    rig.registry.register(
        ToolDefinition(
            name="article.get_claims",
            schema_version="1",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.LOCAL_READ,
            cost_model={},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Lecture locale.",),
            capability_key="investigation.local_read",
        ),
        LocalAdapter(),
    )
    rig.capabilities = frozenset({*rig.capabilities, "investigation.local_read"})

    result = await rig.dispatch(
        "shodan.host",
        {"ip": _SHODAN_IP},
        context=rig._context_for("policy-test", capabilities=rig.capabilities),
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert "article.get_claims" in result.suggested_alternatives
    assert port.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tlp", "do_not_submit"),
    ((TLPLevel.RED, False), (TLPLevel.GREEN, True)),
)
async def test_disclosure_bounds_deny_search_before_external_call(tlp, do_not_submit) -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port)
    rig.envelope = replace(
        rig.envelope,
        disclosure=DisclosureBounds(
            tlp=tlp,
            do_not_submit=do_not_submit,
            external_llm_allowed=True,
        ),
    )

    result = await rig.dispatch(
        "shodan.search",
        {"query": "port:443", "justification": "Comparer la surface indexée.", "evidence_refs": []},
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert port.calls == []


@pytest.mark.asyncio
async def test_observation_records_capture_age_article_ioc_shared_hosting_and_vt_links() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port)
    vt_ref = await _add_vt_relationship(rig)

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.budget_consumed[BudgetDimension.SHODAN_CREDITS] == 1
    observation = await rig.observations.get(result.observation_refs[0])
    assert observation is not None
    assert observation.kind is EvidenceKind.SHODAN_OBSERVATION
    assert observation.observed_at == CAPTURED_AT
    assert any("certificat ou une bannière partagés" in item for item in observation.limitations)
    assert any("cloud/CDN" in item for item in observation.limitations)
    assert any("Réponse brute non archivée" in item for item in observation.limitations)
    payload = rig.store.blobs[observation.blob_id].decode()
    assert '"present":true' in payload
    assert '"ioc_type":"ip"' in payload
    assert str(vt_ref) in payload
    assert '"age_seconds_at_capture":99000.0' in payload
    assert "scan en temps réel" in payload


@pytest.mark.asyncio
async def test_raw_response_archive_is_referenced_by_sha_and_replay_is_idempotent() -> None:
    port = FakeShodanPort()
    store = _BlobStore()
    archive = _RawArchive(store)
    rig = _Rig(shodan=port, raw_archive=archive)
    operation_key = "shodan-raw-archive-replay"

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP}, operation_key=operation_key)
    replay = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP}, operation_key=operation_key)

    raw_observation = await rig.observations.get(result.observation_refs[0])
    summary_observation = await rig.observations.get(result.observation_refs[1])
    assert raw_observation is not None
    assert summary_observation is not None
    assert raw_observation.kind is EvidenceKind.SHODAN_OBSERVATION
    assert summary_observation.kind is EvidenceKind.SHODAN_OBSERVATION
    assert len(archive.calls) == 1
    raw_bytes, bucket, mime_type = archive.calls[0]
    assert bucket == "shodan-raw"
    assert mime_type == "application/json"
    assert raw_bytes == HOST_RESPONSE
    assert b"key=" not in raw_bytes
    assert store.blobs[raw_observation.blob_id] == raw_bytes
    assert raw_observation.sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert raw_observation.sha256 == result.output["raw_sha256"]
    assert result.output["raw_blob_id"] == str(raw_observation.blob_id)
    assert result.output["raw_response_archived"] is True
    assert "summary" in result.output
    assert result.output["summary_observation_id"] == str(summary_observation.id)
    summary_payload = json.loads(rig.store.blobs[summary_observation.blob_id])
    assert summary_payload["summary"]["services"]
    assert replay == result
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_raw_response_over_configured_limit_is_not_archived_and_returns_partial() -> None:
    port = FakeShodanPort()
    store = _BlobStore()
    archive = _RawArchive(store)
    rig = _Rig(shodan=port, raw_archive=archive, max_response_bytes=1)

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.PARTIAL
    assert archive.calls == []
    assert result.output["raw_response_archived"] is False
    observation = await rig.observations.get(result.observation_refs[0])
    assert observation is not None
    assert any("shodan_max_response_bytes" in item for item in observation.limitations)


@pytest.mark.asyncio
async def test_host_output_and_evidence_redact_operational_secrets_and_signed_urls() -> None:
    class SecretPort(FakeShodanPort):
        async def host(self, ip):
            view = await super().host(ip)
            unsafe = replace(
                view.services[0],
                banner=(
                    "Authorization: Bearer OPERATOR_RESEARCH_TOKEN=test-token "
                    "SHODAN_API_KEY=provider-secret api_key=cti-token "
                    "https://x.test/?signature=signed"
                ),
            )
            return replace(view, services=(unsafe, *view.services[1:]))

    port = SecretPort()
    rig = _Rig(shodan=port)

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert "provider-secret" not in str(result.output)
    assert "test-token" not in str(result.output)
    assert "https://x.test" not in str(result.output)
    # D7 keeps authorized CTI material such as a scanned api_key value.
    assert "api_key=cti-token" in str(result.output)
    stored = b" ".join(rig.store.blobs.values())
    assert b"provider-secret" not in stored
    assert b"test-token" not in stored
    assert b"https://x.test" not in stored
    assert b"api_key=cti-token" in stored
    service = result.output["summary"]["services"][0]
    assert service["banner_truncated"] is False


@pytest.mark.asyncio
async def test_registry_replay_does_not_make_a_second_shodan_call() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port)
    args = {"ip": _SHODAN_IP}
    key = tool_operation_key(
        investigation_id=rig.investigation_id,
        tool_name="shodan.host",
        schema_version="1",
        normalized_parameters=args,
        purpose="same stable Shodan operation",
    )

    first = await rig.dispatch(
        "shodan.host", args, operation_key=key, purpose="same stable Shodan operation"
    )
    replay = await rig.dispatch(
        "shodan.host", args, operation_key=key, purpose="same stable Shodan operation"
    )

    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert replay == first
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_domain_and_optional_count_tools_record_exact_objects() -> None:
    port = FakeShodanPort()
    rig = _Rig(shodan=port)

    domain_result = await rig.dispatch("shodan.domain", {"domain": "Example.Test"})
    count_result = await rig.dispatch(
        "shodan.count",
        {"query": "port:443", "justification": "Estimer un pivot.", "evidence_refs": []},
    )

    assert domain_result.status is ToolInvocationStatus.SUCCEEDED
    assert count_result.status is ToolInvocationStatus.SUCCEEDED
    domain_observation = await rig.observations.get(domain_result.observation_refs[0])
    count_observation = await rig.observations.get(count_result.observation_refs[0])
    assert domain_observation is not None and domain_observation.object_id == "example.test"
    assert count_observation is not None and count_observation.object_id == "port:443"


@pytest.mark.asyncio
async def test_transport_error_is_failed_without_echoing_provider_error() -> None:
    class TransportPort(FakeShodanPort):
        async def host(self, ip):
            self.calls.append(("host", (ip,)))
            raise ShodanTransportError("api_key=provider-secret https://api.test/?signature=signed")

    port = TransportPort()
    rig = _Rig(shodan=port)

    result = await rig.dispatch("shodan.host", {"ip": _SHODAN_IP})

    assert result.status is ToolInvocationStatus.FAILED
    assert "provider-secret" not in str(result)
    assert "signature=signed" not in str(result)


def _configure_vertical_fixture(fixture: _Fixture, *, shodan: FakeShodanPort | None) -> None:
    fixture.registry.unregister("shodan.host", "1")
    claim = ArticleClaimV1(
        id="C1",
        statement=(f"IP {_SHODAN_IP} is a C2 for family F during 2025-11-01 to 2025-11-04."),
        source_refs=("article-source",),
    )
    snapshot = replace(
        fixture.snapshot,
        article=ArticleContextV1(
            short_summary="Infrastructure-only claim with no malware sample.",
            claims=(claim,),
            documented_samples=(),
            known_chain=({"kind": "documented_ioc", "artifact_type": "ip", "value": _SHODAN_IP},),
            open_questions=("Is the IP attribution supported?",),
        ),
    )
    fixture.snapshot = snapshot
    fixture.context_builder.article_snapshots = _ArticlePort(snapshot)
    fixture.context_builder.register_local_tools(fixture.registry, _ArticlePort(snapshot))
    store = _BlobStore()
    recorder = ProductionEvidenceRecorder(store, fixture.observations)  # type: ignore[arg-type]
    register_shodan_tools(
        fixture.registry,
        shodan=shodan,
        recorder=recorder,
        article_snapshots=snapshot,
        observations=fixture.observations,
        findings=fixture.findings,
        capabilities=ShodanCapabilities(host=shodan is not None),
    )
    caps = frozenset(
        {
            "investigation.local_read",
            *(shodan_tool_capabilities(ShodanCapabilities(host=True)) if shodan else ()),
        }
    )
    fixture.available_capabilities = caps
    fixture.service._available_capabilities = caps
    fixture.envelope = InvestigationEnvelope(
        investigation_id=fixture.investigation_id,
        allowed_providers=("shodan",),
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 20,
                BudgetDimension.SHODAN_CREDITS: 10,
            }
        ),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-test",
    )


def _qualified_opinion(finding_id: UUID, observation_id: UUID) -> dict[str, object]:
    return {
        "blocks": [
            {
                "block_type": OpinionBlockType.NETWORK_AND_INFRASTRUCTURE.value,
                "text": (
                    "L'attribution de cette IP à la famille F n'est pas démontrée : "
                    "Shodan montre un service cloud/CDN et un certificat partagé, "
                    "avec des captures postérieures à la période rapportée."
                ),
                "finding_refs": [str(finding_id)],
                "evidence_refs": [str(observation_id)],
                "qualification": OpinionClaimQualification.INFERENCE.value,
                "certainty": 0.55,
            }
        ],
        "unresolved_points": [
            "Aucune télémétrie ne relie directement cette IP à la famille "
            "pendant la période de l'attaque."
        ],
    }


def _recorded_observation_id(operation_key: str) -> UUID:
    explicit_key = json.dumps(
        {"explicit": f"shodan:{operation_key}"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(_EVIDENCE_NAMESPACE, explicit_key)


@pytest.mark.asyncio
async def test_d4_vertical_infrastructure_only_qualifies_claim_and_drafts_network_opinion() -> None:
    port = FakeShodanPort()
    fixture = _Fixture(responses=[], tools=(("shodan.host", "shodan"),))
    _configure_vertical_fixture(fixture, shodan=port)
    call = {
        "tool_name": "shodan.host",
        "schema_version": "1",
        "arguments": {"ip": _SHODAN_IP},
        "purpose": "Vérifier l'attribution C2 de l'IP citée par l'article.",
    }
    operation_key, _ = _operation_and_observation_id(fixture.investigation_id, call)
    observation_id = _recorded_observation_id(operation_key)
    proposal = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.QUALIFIED.value,
        "Attribution non démontrée : le certificat et le service sont partagés ; "
        "les captures sont hors fenêtre de l'attaque.",
        observation_id,
        ("C1",),
    )
    finding_id = _finding_id(fixture.investigation_id, proposal)
    transport = ScriptedTurnTransport(
        [
            _decision(
                goal="assess the article's C2 attribution",
                next_action="draft_opinion",
                tools=(call,),
                findings=(proposal,),
                opinion=_qualified_opinion(finding_id, observation_id),
            ),
            _decision(goal="finish with a qualified network opinion", next_action="finish"),
        ]
    )
    fixture.bridge_transport = transport
    fixture.service._bridge = BridgeInvestigator(transport)
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert len(port.calls) == 1
    findings = await fixture.findings.list_for_investigation(fixture.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.QUALIFIED
    assert findings[0].claim_refs == ("C1",)
    assert findings[0].observation_refs == (observation_id,)
    assert any("période de l'attaque" in item for item in findings[0].limitations) or (
        "fenêtre de l'attaque" in findings[0].statement
    )
    opinion = await fixture.opinions.get(uuid5(_OPINION_NAMESPACE, str(fixture.investigation_id)))
    assert opinion is not None
    assert "certificat partagé" in opinion.blocks[0].text


@pytest.mark.asyncio
async def test_d4_shodan_absent_keeps_local_investigation_and_drafts_opinion() -> None:
    fixture = _Fixture(responses=[], tools=(("shodan.host", "shodan"),))
    _configure_vertical_fixture(fixture, shodan=None)
    shodan_call = {
        "tool_name": "shodan.host",
        "schema_version": "1",
        "arguments": {"ip": _SHODAN_IP},
        "purpose": "Vérifier l'IOC réseau; poursuivre localement si Shodan manque.",
    }
    local_call = {
        "tool_name": "article.get_claims",
        "schema_version": "1",
        "arguments": {"claim_ids": ["C1"]},
        "purpose": "Relire localement la claim et préparer un avis prudent.",
    }
    opinion = {
        "blocks": [
            {
                "block_type": OpinionBlockType.NETWORK_AND_INFRASTRUCTURE.value,
                "text": "L'attribution réseau reste non établie faute "
                "d'observation Shodan disponible.",
                "finding_refs": [],
                "evidence_refs": [],
                "qualification": OpinionClaimQualification.HYPOTHESIS.value,
                "certainty": 0.2,
            }
        ],
        "unresolved_points": [
            "Reprendre une vérification passive si la capacité devient disponible."
        ],
    }
    transport = ScriptedTurnTransport(
        [
            _decision(
                goal="keep investigating the local article despite missing Shodan",
                next_action="draft_opinion",
                tools=(shodan_call, local_call),
                opinion=opinion,
            ),
            _decision(goal="finish with the local draft", next_action="finish"),
        ]
    )
    fixture.bridge_transport = transport
    fixture.service._bridge = BridgeInvestigator(transport)
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    shodan_key, _ = _operation_and_observation_id(fixture.investigation_id, shodan_call)
    shodan_invocation = await fixture.invocations.get_by_operation_key(shodan_key)
    assert shodan_invocation is not None
    assert shodan_invocation.status is ToolInvocationStatus.UNAVAILABLE
    local_key, _ = _operation_and_observation_id(fixture.investigation_id, local_call)
    local_invocation = await fixture.invocations.get_by_operation_key(local_key)
    assert local_invocation is not None
    assert local_invocation.status is ToolInvocationStatus.SUCCEEDED
    opinion = await fixture.opinions.get(uuid5(_OPINION_NAMESPACE, str(fixture.investigation_id)))
    assert opinion is not None and opinion.status.value == "draft"
    assert "non établie" in opinion.blocks[0].text

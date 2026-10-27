from __future__ import annotations

import asyncio
import copy
import importlib
import pickle
import time
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from cti_app.application import external_effects as external_effects_module
from cti_app.application.external_effects import (
    Ambiguous,
    Completed,
    ConsumeCommitted,
    ConsumeRefused,
    DefinitelyNotSubmitted,
    ExistingClaim,
    ExternalEffectClaim,
    ExternalEffectCoordinator,
    ExternalEffectReusePolicy,
    HandoffGateRefused,
    NoSubmissionProofKind,
    PhysicalSendCapability,
    Refused,
    ReuseDecision,
    TransportResponse,
    _NoHandoffProofVerifier,
    _PermitRegistry,
)
from cti_app.application.model_gateway import BridgeExternalEffectBinding
from cti_app.domain.external_effects import (
    ConsumedEffectPermit,
    EffectClaimState,
    EvidenceReference,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
    ExternalEffectValidationError,
    FrozenWireRequest,
    PreparedExternalRequest,
    _wire_request_sha256,
    application_request_sha256,
    canonical_json_bytes,
    canonical_request_sha256,
    effect_key,
    occurrence_sha256,
)
from cti_app.infrastructure.database.repositories.external_effects import (
    ExternalEffectFamilyBinding,
    SqlAlchemyExternalEffectClaimStore,
    _ConsumeDenied,
    _family_credentials_match,
)
from cti_app.integrations.models import (
    BridgeTransportError,
    HttpResponsesTransport,
    create_coordinated_http_responses_transport,
)

_INVESTIGATION_ID = UUID("b4079022-263e-48cb-86f7-460be4457fcb")
_INVOCATION_ID = UUID("7f540f99-69eb-49c5-b512-94a297c57208")
_APPROVAL_ID = UUID("b5e03b5a-9619-461f-9a39-10ec2856373d")


def _wire(
    *,
    target: bytes = b"/v1/report?hash=abc",
    headers: tuple[tuple[bytes, bytes], ...] = ((b"accept", b"application/json"),),
    body: bytes = b"",
) -> FrozenWireRequest:
    return FrozenWireRequest(method=b"GET", target=target, headers=headers, body=body)


def _prepared(
    *,
    family: str = "vt.file_report",
    provider: str = "virustotal",
    request: Mapping[str, object] | None = None,
    wire: FrozenWireRequest | None = None,
    occurrence_id: UUID | None = None,
    evidence_refs: tuple[EvidenceReference, ...] = (),
    credential_parameter_names: tuple[str, ...] = (),
) -> PreparedExternalRequest:
    return PreparedExternalRequest.build(
        family=family,
        provider=provider,
        provider_scope="tenant-prod",
        normalizer_version="vt.file-report.v1",
        canonical_request=request or {"hash": "abc", "include": ["relations", "history"]},
        application_request=wire or _wire(),
        occurrence_id=occurrence_id,
        evidence_refs=evidence_refs,
        credential_parameter_names=credential_parameter_names,
    )


def _authority(
    *,
    invocation_id: UUID | None = _INVOCATION_ID,
    amounts: Mapping[str, int] | None = None,
    authority_kind: ExternalEffectAuthorityKind = ExternalEffectAuthorityKind.HUMAN_G2,
    policy: bool = False,
) -> ExternalEffectAuthority:
    if policy:
        authority_kind = ExternalEffectAuthorityKind.OSINT_POLICY
    return ExternalEffectAuthority(
        investigation_id=_INVESTIGATION_ID,
        invocation_id=invocation_id,
        operation_key="op-1",
        reservation_amounts=amounts or {"vt_units": 1},
        expected_envelope_version=4,
        approval_decision_id=_APPROVAL_ID,
        invocation_arguments_sha256="a" * 64 if invocation_id else None,
        authority_kind=authority_kind,
        policy_event_id=uuid4() if policy else None,
        policy_version="osint-open.v1" if policy else None,
        pinned_subject_id=uuid4() if policy else None,
    )


class FakeStore:
    def __init__(self, result: object | None = None) -> None:
        self.result = result or ConsumeCommitted(uuid4())
        self.events: list[str] = []
        self.complete_result = True
        self.completed_outcomes: list[str | None] = []
        self.completed_result_refs: list[str | None] = []
        self.sealed: list[tuple[str, str]] = []
        self.reuse_id: UUID | None = uuid4()
        self.reuse_calls: list[ReuseDecision] = []
        self.refused_reuse_reservations: list[str] = []
        self._first_consume = True
        self.claim_state = EffectClaimState.CONSUMED
        self.handoff_nonce: UUID | None = None
        self.gate_entries = 0
        self.handoff_delay = 0.0
        self._prepared_request: PreparedExternalRequest | None = None
        self._proof_verifier: _NoHandoffProofVerifier | None = None
        self.no_handoff_proofs: list[object] = []

    def _bind_no_handoff_proof_verifier(self, verifier: _NoHandoffProofVerifier) -> None:
        if type(verifier) is not _NoHandoffProofVerifier:
            raise TypeError("fake store requires a coordinator-bound verifier")
        if self._proof_verifier is not None:
            raise ValueError("fake store already has a no-handoff proof verifier")
        verifier.bind_store(self)
        self._proof_verifier = verifier

    async def consume(
        self,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        **kwargs: object,
    ) -> object:
        self.events.append("commit")
        self._prepared_request = prepared
        if not self._first_consume and isinstance(self.result, ConsumeCommitted):
            return ExistingClaim(
                ExternalEffectClaim(
                    id=self.result.claim_id,
                    investigation_id=authority.investigation_id,
                    invocation_id=authority.invocation_id,
                    effect_key=effect_key(authority.investigation_id, prepared),
                    family=prepared.family,
                    provider=prepared.provider,
                    provider_scope=prepared.provider_scope,
                    canonical_request_sha256=prepared.canonical_request_sha256,
                    application_request_sha256=prepared.application_request_sha256,
                    envelope_version=authority.expected_envelope_version,
                    approval_decision_id=authority.approval_decision_id,
                    state=self.claim_state,
                    result_ref=(
                        self.completed_result_refs[-1] if self.completed_result_refs else None
                    ),
                    bridge_turn_id=authority.bridge_turn_id,
                    model_run_id=authority.model_run_id,
                    outcome_code=(self.completed_outcomes[-1] if self.completed_outcomes else None),
                )
            )
        self._first_consume = False
        return self.result

    async def complete(self, *args: object, **kwargs: object) -> bool:
        self.events.append("complete")
        self.completed_outcomes.append(kwargs.get("outcome_code"))  # type: ignore[arg-type]
        self.completed_result_refs.append(kwargs.get("result_ref"))  # type: ignore[arg-type]
        if self.complete_result:
            self.claim_state = EffectClaimState.COMPLETED
        return self.complete_result

    async def enter_handoff(
        self,
        claim_id: UUID,
        *,
        permit_nonce: UUID,
        request_digest: str,
        expires_at_monotonic: float,
    ) -> bool:
        self.events.append("gate")
        if (
            self.claim_state is not EffectClaimState.CONSUMED
            or self.handoff_nonce is not None
            or self.result.claim_id != claim_id  # type: ignore[union-attr]
            or self._prepared_request is None
            or request_digest != self._prepared_request.application_request_sha256
            or time.monotonic() >= expires_at_monotonic
        ):
            return False
        self.handoff_nonce = permit_nonce
        self.gate_entries += 1
        if self.handoff_delay:
            await asyncio.sleep(self.handoff_delay)
        return True

    async def definitely_not_submitted(
        self, claim_id: UUID, *, proof: object, reason_code: str
    ) -> bool:
        self.no_handoff_proofs.append(proof)
        proof_kind = (
            self._proof_verifier.consume(self, proof, claim_id)
            if self._proof_verifier is not None
            else None
        )
        if proof_kind is None:
            return False
        if (
            self.claim_state is not EffectClaimState.CONSUMED
            or proof_kind is not NoSubmissionProofKind.BEFORE_HANDOFF
            or self.handoff_nonce is not None
            or self.result.claim_id != claim_id  # type: ignore[union-attr]
        ):
            return False
        self.sealed.append(("not_submitted", reason_code))
        self.claim_state = EffectClaimState.DEFINITIVELY_NOT_SUBMITTED
        return True

    async def needs_review(self, claim_id: UUID, *, reason_code: str) -> bool:
        self.sealed.append(("needs_review", reason_code))
        if self.claim_state is EffectClaimState.CONSUMED:
            self.claim_state = EffectClaimState.NEEDS_REVIEW
        return True

    async def record_reuse(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        decision: ReuseDecision,
    ) -> UUID | None:
        self.reuse_calls.append(decision)
        return self.reuse_id

    async def release_refused_reuse_reservation(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> bool:
        if authority.invocation_id is not None and authority.invocation_id != claim.invocation_id:
            self.refused_reuse_reservations.append(authority.operation_key)
            return True
        return False


class FakeTransport:
    def __init__(
        self,
        events: list[str] | None = None,
        response: object | Exception | None = None,
        *,
        fail_during_submission: bool = False,
        send_delay_seconds: float = 0.0,
        substitute_request: bool = False,
        retry_inside_transport: bool = False,
        client_retries_enabled: bool = False,
    ) -> None:
        self.__raw_client = _FakeRawClient(events, response, retries_enabled=client_retries_enabled)
        self.submit_entries = 0
        self.events = events
        self.fail_during_submission = fail_during_submission
        self.send_delay_seconds = send_delay_seconds
        self.substitute_request = substitute_request
        self.retry_inside_transport = retry_inside_transport

    @property
    def calls(self) -> list[FrozenWireRequest]:
        return self.__raw_client.calls

    @property
    def physical_calls(self) -> int:
        return len(self.__raw_client.calls)

    async def submit_once(
        self,
        request: FrozenWireRequest,
        capability: PhysicalSendCapability | None = None,
    ) -> TransportResponse:
        self.submit_entries += 1
        if (
            not isinstance(capability, PhysicalSendCapability)
            or type(capability) is not PhysicalSendCapability
        ):
            raise HandoffGateRefused("physical send capability is required")
        if self.fail_during_submission:
            raise RuntimeError("local transport setup failed")
        if self.send_delay_seconds:
            await asyncio.sleep(self.send_delay_seconds)
        if self.__raw_client.retries_enabled:
            raise HandoffGateRefused("raw client retries must be disabled")
        sent_request = _wire(target=b"/substituted") if self.substitute_request else request
        capability(sent_request)
        response = await self.__raw_client.send(sent_request, follow_redirects=False)
        if self.retry_inside_transport:
            capability(request)
        return response


class _FakeRawClient:
    def __init__(
        self,
        events: list[str] | None,
        response: object | Exception | None,
        *,
        retries_enabled: bool = False,
    ) -> None:
        self.calls: list[FrozenWireRequest] = []
        self.events = events
        self.response = response or TransportResponse(result_ref="artifact:result-1")
        self.retries_enabled = retries_enabled

    async def send(
        self, request: FrozenWireRequest, *, follow_redirects: bool
    ) -> TransportResponse:
        if follow_redirects or self.retries_enabled:
            raise AssertionError("fake raw client must disable redirects and retries")
        self.calls.append(request)
        if self.events is not None:
            assert self.events[-1] == "gate"
            self.events.append("send")
        if isinstance(self.response, Exception):
            raise self.response
        return self.response  # type: ignore[return-value]


def _coordinator(
    store: Any,
    *,
    reuse_policies: Mapping[str, ExternalEffectReusePolicy] | None = None,
    permit_ttl_seconds: float = 5.0,
) -> ExternalEffectCoordinator:
    return ExternalEffectCoordinator(
        store,
        reuse_policies=reuse_policies,
        permit_ttl_seconds=permit_ttl_seconds,
    )


def _bridge_external_effect_binding() -> BridgeExternalEffectBinding:
    bridge_turn_id = uuid4()
    model_run_id = uuid4()
    authority = replace(
        _authority(),
        bridge_turn_id=bridge_turn_id,
        model_run_id=model_run_id,
    )
    return BridgeExternalEffectBinding(
        authority=authority,
        family="bridge.investigation_turn.v1",
        provider_scope="bridge-test",
        normalizer_version="bridge.turn.v1",
        bridge_turn_id=bridge_turn_id,
        model_run_id=model_run_id,
        model_input_sha256="a" * 64,
        authorized_input_hash="b" * 64,
    )


@pytest.mark.asyncio
async def test_coordinated_bridge_posts_the_same_json_bytes_and_hides_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"model": "chatgpt-web", "input": "Exact authorized prompt bytes."}
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"id":"resp-test","status":"completed","output_text":"ok"}',
        )

    key = "authorization-probe-do-not-persist"
    legacy = HttpResponsesTransport(
        "https://bridge.test/v1",
        api_key=key,
        http_transport=httpx.MockTransport(handler),
    )
    await legacy.create(payload, idempotency_key="bridge-request-1")
    legacy_body = captured[-1].content

    send_calls: list[httpx.Request] = []
    original_send = httpx.AsyncClient.send

    async def counted_send(
        client: httpx.AsyncClient,
        request: httpx.Request,
        **kwargs: Any,
    ) -> httpx.Response:
        send_calls.append(request)
        return await original_send(client, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", counted_send)

    store = FakeStore()
    gated = create_coordinated_http_responses_transport(
        "https://bridge.test/v1",
        api_key=key,
        coordinator=_coordinator(store),
        http_transport=httpx.MockTransport(handler),
    )
    result = await gated.create(
        payload,
        idempotency_key="bridge-request-1",
        bridge_external_effect_binding=_bridge_external_effect_binding(),
    )

    assert result["output_text"] == "ok"
    assert len(captured) == 2
    assert len(send_calls) == 1
    assert send_calls[0] is captured[1]
    assert captured[1].content == legacy_body
    assert store._prepared_request is not None
    wire = store._prepared_request.application_request
    assert store._prepared_request.provider == "bridge"
    assert wire.method == b"POST"
    assert wire.target == b"/v1/responses"
    assert wire.body == legacy_body
    transmitted = send_calls[0]
    assert wire.method == transmitted.method.encode("ascii")
    assert wire.target == transmitted.url.raw_path
    assert wire.headers == tuple(transmitted.headers.raw)
    assert wire.body == transmitted.content
    assert any(
        name.lower() == b"authorization" and value.endswith(key.encode())
        for name, value in wire.headers
    )
    assert key not in repr(
        {
            "application_request_sha256": store._prepared_request.application_request_sha256,
            "canonical_request": store._prepared_request.canonical_request,
            "result_ref": store.completed_result_refs,
        }
    )


@pytest.mark.asyncio
async def test_production_bridge_allows_unbound_request_but_investigation_gate_refuses_it() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"status": "completed", "output_text": "ok"})

    production = HttpResponsesTransport(
        "https://bridge.test/v1",
        api_key=None,
        http_transport=httpx.MockTransport(handler),
    )
    result = await production.create({"input": "production-style request"})
    assert result["output_text"] == "ok"
    assert len(calls) == 1

    store = FakeStore()
    gated = create_coordinated_http_responses_transport(
        "https://bridge.test/v1",
        api_key=None,
        coordinator=_coordinator(store),
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(BridgeTransportError) as missing_binding:
        await gated.create({"input": "must not send"}, idempotency_key="bridge-request-1")
    assert missing_binding.value.code == "external_effect_authority_required"
    for path in ("/responses", "/responses/", "/responses?mode=retry", "/responses/../responses"):
        with pytest.raises(BridgeTransportError) as legacy_path:
            await gated._request("POST", path, json_body={"input": "must not send"})
        assert legacy_path.value.code == "external_effect_authority_required"
    assert len(calls) == 1
    assert store.events == []


@pytest.mark.asyncio
async def test_complete_bridge_429_is_replayed_without_a_second_post() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "2"},
            json={"detail": {"code": "bridge_rate_limited", "message": "response body"}},
        )

    store = FakeStore()
    coordinator = _coordinator(store)
    transport = create_coordinated_http_responses_transport(
        "https://bridge.test/v1",
        api_key="authorization-probe-do-not-persist",
        coordinator=coordinator,
        http_transport=httpx.MockTransport(handler),
    )
    kwargs = {
        "payload": {"input": "safe test prompt"},
        "idempotency_key": "bridge-request-429",
        "bridge_external_effect_binding": _bridge_external_effect_binding(),
    }
    for _ in range(2):
        with pytest.raises(BridgeTransportError) as caught:
            await transport.create(**kwargs)
        assert caught.value.code == "bridge_rate_limited"
        assert caught.value.retry_after == 2
        assert caught.value.retryable is True
        assert caught.value.submission_state == "post_submission"
        assert caught.value.completed_response is True
        assert "authorization-probe-do-not-persist" not in str(caught.value)
    assert len(calls) == 1
    assert store.completed_outcomes == ["bridge_rate_limited"]
    assert store.claim_state is EffectClaimState.COMPLETED


@pytest.mark.asyncio
async def test_bridge_500_after_handoff_needs_review_and_never_resends() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"error": "synthetic server failure"})

    store = FakeStore()
    transport = create_coordinated_http_responses_transport(
        "https://bridge.test/v1",
        api_key="authorization-probe-do-not-persist",
        coordinator=_coordinator(store),
        http_transport=httpx.MockTransport(handler),
    )
    kwargs = {
        "payload": {"input": "safe test prompt"},
        "idempotency_key": "bridge-request-server-error",
        "bridge_external_effect_binding": _bridge_external_effect_binding(),
    }
    for _ in range(2):
        with pytest.raises(BridgeTransportError) as caught:
            await transport.create(**kwargs)
        assert caught.value.code == "model_submission_reconciliation_required"
        assert caught.value.submission_state == "external_state_unknown"
    assert len(calls) == 1
    assert store.sealed == [("needs_review", "transport_outcome_unknown")]


@pytest.mark.asyncio
async def test_timeout_after_bridge_handoff_is_needs_review_and_never_resends() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("authorization-probe-do-not-persist", request=request)

    store = FakeStore()
    transport = create_coordinated_http_responses_transport(
        "https://bridge.test/v1",
        api_key="authorization-probe-do-not-persist",
        coordinator=_coordinator(store),
        http_transport=httpx.MockTransport(handler),
    )
    kwargs = {
        "payload": {"input": "safe test prompt"},
        "idempotency_key": "bridge-request-timeout",
        "bridge_external_effect_binding": _bridge_external_effect_binding(),
    }
    for _ in range(2):
        with pytest.raises(BridgeTransportError) as caught:
            await transport.create(**kwargs)
        assert caught.value.code == "model_submission_reconciliation_required"
        assert caught.value.submission_state == "external_state_unknown"
        assert "authorization-probe-do-not-persist" not in str(caught.value)
    assert len(calls) == 1
    assert store.sealed == [("needs_review", "transport_outcome_unknown")]


class AllowReuse:
    def __init__(self, source: UUID | None, refs: tuple[EvidenceReference, ...]) -> None:
        self.decision = ReuseDecision(source_invocation_id=source, observation_refs=refs)

    async def authorize_reuse(
        self,
        claim: ExternalEffectClaim,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
    ) -> ReuseDecision:
        return self.decision


def test_digest_determinism_and_mutation_sensitivity() -> None:
    one = {"z": [2, True], "a": {"b": "value"}}
    two = {"a": {"b": "value"}, "z": [2, True]}
    assert canonical_json_bytes(one) == canonical_json_bytes(two)
    assert canonical_request_sha256(one) == canonical_request_sha256(two)
    assert canonical_request_sha256(
        {"a": {"b": "changed"}, "z": [2, True]}
    ) != canonical_request_sha256(one)
    assert application_request_sha256(_wire()) == application_request_sha256(_wire())
    assert application_request_sha256(
        _wire(target=b"/v1/report?hash=changed")
    ) != application_request_sha256(_wire())
    assert application_request_sha256(
        _wire(headers=((b"Content-Type", b"application/json"), (b"Accept", b"application/json")))
    ) != application_request_sha256(
        _wire(headers=((b"Accept", b"application/json"), (b"Content-Type", b"application/json")))
    )


def test_credential_redacted_digest_hides_credentials_but_exact_digest_does_not() -> None:
    first = _wire(
        target=b"/responses?api_key=first&hash=abc#refresh_token=first",
        headers=((b"Authorization", b"Bearer first"), (b"x_api-key", b"first")),
    )
    second = _wire(
        target=b"/responses?api_key=second&hash=abc#refresh_token=second",
        headers=((b"Authorization", b"Bearer second"), (b"x_api-key", b"second")),
    )
    assert application_request_sha256(first) == application_request_sha256(second)
    assert _wire_request_sha256(first) != _wire_request_sha256(second)
    prepared = _prepared(wire=first)
    assert _wire_request_sha256(first) not in repr(prepared)
    changed_target = _wire(
        target=b"/responses?api_key=first&hash=changed#refresh_token=first",
        headers=first.headers,
    )
    assert application_request_sha256(changed_target) != application_request_sha256(first)


def test_declared_credential_cannot_be_persisted_in_canonical_request() -> None:
    with pytest.raises(ExternalEffectValidationError, match=r"canonical_request\.key"):
        PreparedExternalRequest.build(
            family="vt.file_report",
            provider="virustotal",
            provider_scope="tenant-prod",
            normalizer_version="vt.file-report.v1",
            canonical_request={"hash": "abc", "key": "SECRET"},
            application_request=_wire(target=b"/v1/report?hash=abc&key=SECRET"),
            credential_parameter_names=("key",),
        )


def test_persisted_header_digest_allowlists_only_documented_safe_values() -> None:
    safe_first = _wire(
        headers=((b"Content-Type", b"application/json"), (b"Accept", b"application/json"))
    )
    safe_second = _wire(
        headers=((b"Content-Type", b"text/plain"), (b"Accept", b"application/json"))
    )
    assert application_request_sha256(safe_first) != application_request_sha256(safe_second)

    aliases_first = _wire(
        headers=(
            (b"Api-Key", b"first-api-key"),
            (b"X-Auth-Token", b"first-auth-token"),
            (b"X-Whatever-Secret", b"first-arbitrary-secret"),
        )
    )
    aliases_second = _wire(
        headers=(
            (b"Api-Key", b"second-api-key"),
            (b"X-Auth-Token", b"second-auth-token"),
            (b"X-Whatever-Secret", b"second-arbitrary-secret"),
        )
    )
    assert application_request_sha256(aliases_first) == application_request_sha256(aliases_second)
    assert _wire_request_sha256(aliases_first) != _wire_request_sha256(aliases_second)
    reordered = _wire(
        headers=(
            (b"X-Whatever-Secret", b"value"),
            (b"Accept", b"text/plain"),
        )
    )
    original_order = _wire(
        headers=(
            (b"Accept", b"text/plain"),
            (b"X-Whatever-Secret", b"value"),
        )
    )
    assert application_request_sha256(reordered) != application_request_sha256(original_order)


@pytest.mark.asyncio
async def test_connect_error_after_handoff_is_ambiguous_and_never_retried() -> None:
    class FabricatedConnectFailure:
        def __init__(self, failure: httpx.ConnectError) -> None:
            self.failure = failure
            self.submit_entries = 0
            self.physical_calls = 0

        async def submit_once(
            self, request: FrozenWireRequest, capability: PhysicalSendCapability
        ) -> TransportResponse:
            self.submit_entries += 1
            capability(request)
            self.physical_calls += 1
            raise self.failure

    store = FakeStore()
    failure = httpx.ConnectError("connection could not be established")
    failed_transport = FabricatedConnectFailure(failure)
    coordinator = _coordinator(store)
    first = await coordinator.send_one(_authority(), _prepared(), failed_transport)
    assert isinstance(first, Ambiguous)
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW
    assert store.handoff_nonce is not None
    assert failed_transport.physical_calls == 1

    replay = await coordinator.send_one(_authority(), _prepared(), failed_transport)
    assert isinstance(replay, Ambiguous)
    assert failed_transport.submit_entries == 1
    assert failed_transport.physical_calls == 1
    assert replay.claim_id == first.claim_id


@pytest.mark.parametrize(
    "parameter", ["tokenizer", "keyword", "signature_version", "access_keyword"]
)
def test_lookalike_query_names_keep_distinct_digests_and_effect_keys(parameter: str) -> None:
    fast_wire = _wire(target=f"/responses?{parameter}=fast".encode())
    slow_wire = _wire(target=f"/responses?{parameter}=slow".encode())
    fast = _prepared(request={parameter: "fast"}, wire=fast_wire)
    slow = _prepared(request={parameter: "slow"}, wire=slow_wire)

    assert fast.application_request_sha256 != slow.application_request_sha256
    assert effect_key(_INVESTIGATION_ID, fast) != effect_key(_INVESTIGATION_ID, slow)


def test_credential_query_values_collapse_but_family_declared_key_is_optional() -> None:
    first_wire = _wire(target=b"/responses?apikey=first&api_key=first&token=first")
    second_wire = _wire(target=b"/responses?apikey=second&api_key=second&token=second")
    first = _prepared(wire=first_wire)
    second = _prepared(wire=second_wire)
    assert first.application_request_sha256 == second.application_request_sha256
    assert effect_key(_INVESTIGATION_ID, first) == effect_key(_INVESTIGATION_ID, second)

    key_first = _wire(target=b"/responses?key=first")
    key_second = _wire(target=b"/responses?key=second")
    assert application_request_sha256(key_first) != application_request_sha256(key_second)
    assert _wire_request_sha256(key_first) != _wire_request_sha256(key_second)
    assert application_request_sha256(
        key_first, credential_parameter_names=("key",)
    ) == application_request_sha256(key_second, credential_parameter_names=("key",))
    assert _prepared(
        wire=key_first, credential_parameter_names=("KEY",)
    ).credential_parameter_names == ("key",)

    declared = ExternalEffectFamilyBinding(
        provider="virustotal",
        provider_scopes=frozenset({"tenant-prod"}),
        normalizer_versions=frozenset({"vt.file-report.v1"}),
        invocation_tool_binding=None,
        credential_parameter_names=frozenset({"KEY"}),
    )
    undeclared = ExternalEffectFamilyBinding(
        provider="virustotal",
        provider_scopes=frozenset({"tenant-prod"}),
        normalizer_versions=frozenset({"vt.file-report.v1"}),
        invocation_tool_binding=None,
    )
    key_prepared = _prepared(wire=key_first, credential_parameter_names=("key",))
    assert declared.credential_parameter_names == frozenset({"key"})
    assert _family_credentials_match(key_prepared, declared)
    assert not _family_credentials_match(key_prepared, undeclared)

    apikey_declared = ExternalEffectFamilyBinding(
        provider="virustotal",
        provider_scopes=frozenset({"tenant-prod"}),
        normalizer_versions=frozenset({"vt.file-report.v1"}),
        invocation_tool_binding=None,
        credential_parameter_names=frozenset({"apikey"}),
    )
    apikey_wire = _wire(target=b"/responses?apikey=first")
    apikey_second_wire = _wire(target=b"/responses?apikey=second")
    assert application_request_sha256(
        apikey_wire, credential_parameter_names=("apikey",)
    ) == application_request_sha256(apikey_second_wire, credential_parameter_names=("apikey",))
    apikey_prepared = _prepared(wire=apikey_wire, credential_parameter_names=("apikey",))
    assert apikey_declared.credential_parameter_names == frozenset({"apikey"})
    assert _family_credentials_match(apikey_prepared, apikey_declared)


@pytest.mark.parametrize("invalid_name", ["api key", "apikey&token", "api%2Dkey"])
def test_family_credential_declarations_must_be_plain_parameter_tokens(invalid_name: str) -> None:
    with pytest.raises(ValueError, match="invalid family credential parameter declaration"):
        ExternalEffectFamilyBinding(
            provider="virustotal",
            provider_scopes=frozenset({"tenant-prod"}),
            normalizer_versions=frozenset({"vt.file-report.v1"}),
            invocation_tool_binding=None,
            credential_parameter_names=frozenset({invalid_name}),
        )


@pytest.mark.parametrize(
    "parameter",
    [
        "apikey",
        "api_key",
        "access_token",
        "id_token",
        "refresh_token",
        "token",
        "client_secret",
        "secret",
        "password",
        "signature",
        "sig",
        "x-amz-signature",
        "x-goog-signature",
        "access_key",
        "x_api_key",
        "api-key",
        "auth_token",
        "x_auth_token",
        "session_token",
        "private_token",
    ],
)
def test_exact_credential_query_and_fragment_aliases_are_redacted(parameter: str) -> None:
    normalized_alias = parameter.upper().replace("_", "-")
    first = _wire(target=f"/responses?{normalized_alias}=first#{normalized_alias}=first".encode())
    second = _wire(
        target=f"/responses?{normalized_alias}=second#{normalized_alias}=second".encode()
    )
    assert application_request_sha256(first) == application_request_sha256(second)


@pytest.mark.parametrize(
    "header",
    [b"Authorization", b"PrOxY_Authorization", b"Cookie", b"x-apikey", b"X-API_key"],
)
def test_exact_credential_headers_are_redacted(header: bytes) -> None:
    first = _wire(headers=((header, b"first"),))
    second = _wire(headers=((header, b"second"),))
    assert application_request_sha256(first) == application_request_sha256(second)


def test_non_allowlisted_header_values_are_redacted_but_names_are_not() -> None:
    first = _wire(headers=((b"authorization-version", b"first"),))
    second = _wire(headers=((b"authorization-version", b"second"),))
    assert application_request_sha256(first) == application_request_sha256(second)
    assert _wire_request_sha256(first) != _wire_request_sha256(second)
    assert application_request_sha256(first) != application_request_sha256(
        _wire(headers=((b"authorization-version-2", b"first"),))
    )

    encoded_alias_first = _wire(headers=((b"x%2Dapikey", b"first"),))
    encoded_alias_second = _wire(headers=((b"x%2Dapikey", b"second"),))
    assert application_request_sha256(encoded_alias_first) == application_request_sha256(
        encoded_alias_second
    )


@pytest.mark.asyncio
async def test_rate_limit_is_completed_and_new_occurrence_retry_is_a_new_claim() -> None:
    first_prepared = _prepared()
    first_store = FakeStore()
    first_transport = FakeTransport(response=TransportResponse(outcome_code="bridge_rate_limited"))
    first = await _coordinator(first_store).send_one(_authority(), first_prepared, first_transport)
    assert isinstance(first, Completed)
    assert first.outcome_code == "bridge_rate_limited"
    assert first_store.completed_outcomes == ["bridge_rate_limited"]
    assert first_transport.physical_calls == 1

    retry_prepared = _prepared(occurrence_id=uuid4())
    retry_store = FakeStore()
    retry_transport = FakeTransport()
    retry = await _coordinator(retry_store).send_one(_authority(), retry_prepared, retry_transport)
    assert isinstance(retry, Completed)
    assert retry.claim_id != first.claim_id
    assert retry_transport.physical_calls == 1

    first_claim = ExternalEffectClaim(
        id=first.claim_id,
        investigation_id=_INVESTIGATION_ID,
        invocation_id=_INVOCATION_ID,
        effect_key=effect_key(_INVESTIGATION_ID, first_prepared),
        family=first_prepared.family,
        provider=first_prepared.provider,
        provider_scope=first_prepared.provider_scope,
        canonical_request_sha256=first_prepared.canonical_request_sha256,
        application_request_sha256=first_prepared.application_request_sha256,
        envelope_version=4,
        approval_decision_id=_APPROVAL_ID,
        state=EffectClaimState.COMPLETED,
        outcome_code="bridge_rate_limited",
    )
    replay_store = FakeStore(ExistingClaim(first_claim))
    replay_transport = FakeTransport()
    replay = await _coordinator(replay_store).send_one(
        _authority(), first_prepared, replay_transport
    )
    assert isinstance(replay, Completed)
    assert replay.outcome_code == "bridge_rate_limited"
    assert replay_transport.physical_calls == 0


@pytest.mark.asyncio
async def test_consumed_claim_replay_is_sealed_needs_review_without_send() -> None:
    prepared = _prepared()
    claim = ExternalEffectClaim(
        id=uuid4(),
        investigation_id=_INVESTIGATION_ID,
        invocation_id=_INVOCATION_ID,
        effect_key=effect_key(_INVESTIGATION_ID, prepared),
        family=prepared.family,
        provider=prepared.provider,
        provider_scope=prepared.provider_scope,
        canonical_request_sha256=prepared.canonical_request_sha256,
        application_request_sha256=prepared.application_request_sha256,
        envelope_version=4,
        approval_decision_id=_APPROVAL_ID,
        state=EffectClaimState.CONSUMED,
    )
    store = FakeStore(ExistingClaim(claim))
    transport = FakeTransport()
    outcome = await _coordinator(store).send_one(_authority(), prepared, transport)
    assert isinstance(outcome, Ambiguous)
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW
    assert store.sealed == [("needs_review", "replayed_consumed_claim")]
    assert transport.physical_calls == 0


@pytest.mark.asyncio
async def test_transport_payload_is_carried_in_memory_and_hidden_from_repr() -> None:
    payload = b"model response bytes that stay in memory"
    store = FakeStore()
    outcome = await _coordinator(store).send_one(
        _authority(), _prepared(), FakeTransport(response=TransportResponse(payload=payload))
    )
    assert isinstance(outcome, Completed)
    assert outcome.payload == payload
    assert payload.decode() not in repr(outcome)
    assert payload.decode() not in repr(TransportResponse(payload=payload))


def test_occurrence_default_and_explicit_are_stable_and_distinct() -> None:
    assert (
        occurrence_sha256(None)
        == "1fa53c303e03ce108082fc0b48f59f997ba2e3a303da05e763d09d6ce145fab7"
    )
    occurrence = UUID("30ecf881-65e8-4a9d-a20b-8c33f1ee62ad")
    assert occurrence_sha256(occurrence) == occurrence_sha256(occurrence)
    assert occurrence_sha256(occurrence) != occurrence_sha256(None)
    assert effect_key(_INVESTIGATION_ID, _prepared()) == effect_key(_INVESTIGATION_ID, _prepared())
    assert effect_key(_INVESTIGATION_ID, _prepared(occurrence_id=occurrence)) != effect_key(
        _INVESTIGATION_ID, _prepared()
    )


@pytest.mark.parametrize("value", [{"x": float("nan")}, {"x": float("inf")}, {"x": -float("inf")}])
def test_canonical_json_rejects_nan_and_infinity(value: object) -> None:
    with pytest.raises(ExternalEffectValidationError):
        canonical_json_bytes(value)


def test_canonical_json_rejects_decimal_quantities_and_invalid_unicode() -> None:
    with pytest.raises(ExternalEffectValidationError):
        canonical_json_bytes({"quantity": 1.25})
    with pytest.raises(ExternalEffectValidationError):
        canonical_json_bytes({"invalid": "\ud800"})


def test_effect_key_binds_investigation_and_provider_scope() -> None:
    base = _prepared()
    assert effect_key(_INVESTIGATION_ID, base) != effect_key(uuid4(), base)
    other_scope = PreparedExternalRequest.build(
        family=base.family,
        provider=base.provider,
        provider_scope="tenant-other",
        normalizer_version=base.normalizer_version,
        canonical_request={"hash": "abc", "include": ["relations", "history"]},
        application_request=_wire(),
    )
    assert effect_key(_INVESTIGATION_ID, base) != effect_key(_INVESTIGATION_ID, other_scope)


@pytest.mark.asyncio
async def test_refusal_mints_no_permit_and_never_sends() -> None:
    store = FakeStore(ConsumeRefused("provider_not_allowed"))
    transport = FakeTransport()
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Refused)
    assert outcome.reason == "provider_not_allowed"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_committed_consume_mints_one_permit_and_calls_transport_once() -> None:
    store = FakeStore()
    transport = FakeTransport(store.events)
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Completed), (store.events, store.sealed)
    assert store.events == ["commit", "gate", "send", "complete"]
    assert len(transport.calls) == 1
    assert store.gate_entries == 1
    request = transport.calls[0]
    assert request == _prepared().application_request
    assert transport.submit_entries == 1
    assert not hasattr(transport, "prepare_submission")


@pytest.mark.asyncio
async def test_forged_exact_type_capability_is_refused_without_sending() -> None:
    transport = FakeTransport()
    forged = object.__new__(PhysicalSendCapability)

    with pytest.raises(AttributeError):
        object.__setattr__(forged, "_PhysicalSendCapability__execute", lambda _request: None)
    with pytest.raises(AttributeError):
        object.__setattr__(forged, "unexpected", object())
    with pytest.raises(HandoffGateRefused, match="not registered"):
        await transport.submit_once(_wire(), forged)

    assert transport.submit_entries == 1
    assert transport.physical_calls == 0


@pytest.mark.asyncio
async def test_capability_copies_are_unregistered_and_surfaces_hide_registry() -> None:
    class _CopyProbeTransport(FakeTransport):
        capability: PhysicalSendCapability | None = None
        copies: tuple[PhysicalSendCapability, ...] = ()

        async def submit_once(
            self,
            request: FrozenWireRequest,
            capability: PhysicalSendCapability | None = None,
        ) -> TransportResponse:
            assert isinstance(capability, PhysicalSendCapability)
            self.capability = capability
            self.copies = (copy.copy(capability), copy.deepcopy(capability))
            for copied_capability in self.copies:
                with pytest.raises(HandoffGateRefused, match="not registered"):
                    copied_capability(request)
            with pytest.raises(TypeError):
                pickle.dumps(capability)
            return await super().submit_once(request, capability)

    store = FakeStore()
    coordinator = _coordinator(store)
    transport = _CopyProbeTransport()
    outcome = await coordinator.send_one(_authority(), _prepared(), transport)

    assert isinstance(outcome, Completed)
    assert transport.capability is not None
    assert transport.physical_calls == 1
    with pytest.raises(HandoffGateRefused, match="not registered"):
        transport.capability(_wire())
    for copied_capability in transport.copies:
        with pytest.raises(HandoffGateRefused, match="not registered"):
            copied_capability(_wire())

    shallow_surfaces = (
        transport.capability,
        transport,
        coordinator,
        external_effects_module,
    )
    hidden_names = {
        "authorizers",
        "authorizer",
        "validator",
        "validators",
        "capability_issuer",
        "capability_revoker",
        "capability_validator",
        "capability_registry",
        "_issue_physical_send_capability",
        "_revoke_physical_send_capability",
        "_install_physical_send_capability_dispatch",
        "_bind_capability_init",
        "physical_send_capability",
    }
    for surface in shallow_surfaces:
        names = set(dir(surface))
        try:
            names.update(vars(surface))
        except TypeError:
            # Slotted capabilities have no instance dictionary.
            assert surface is transport.capability
        assert not names.intersection(hidden_names)
        assert not any("capability_registry" in name.lower() for name in names)
        assert not any("validator" in name.lower() for name in names)


@pytest.mark.parametrize("submit_behavior", ("return_unused", "return_after_refusal", "raise"))
@pytest.mark.asyncio
async def test_saved_capability_is_revoked_when_submit_once_exits(
    submit_behavior: str,
) -> None:
    class _CaptureCapabilityTransport:
        def __init__(self) -> None:
            self.submit_entries = 0
            self.capability: PhysicalSendCapability | None = None
            self.request: FrozenWireRequest | None = None
            self.physical_calls = 0

        async def submit_once(
            self,
            request: FrozenWireRequest,
            capability: PhysicalSendCapability | None = None,
        ) -> TransportResponse:
            self.submit_entries += 1
            assert isinstance(capability, PhysicalSendCapability)
            self.capability = capability
            self.request = request
            if submit_behavior == "raise":
                raise RuntimeError("transport setup failed")
            if submit_behavior == "return_after_refusal":
                try:
                    capability(_wire(target=b"/substituted"))
                except HandoffGateRefused:
                    pass
            return TransportResponse(result_ref="artifact:unused-capability")

        def send_with_saved_capability(self) -> None:
            assert self.capability is not None
            assert self.request is not None
            self.capability(self.request)
            self.physical_calls += 1

    store = FakeStore()
    transport = _CaptureCapabilityTransport()
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)

    assert isinstance(outcome, Ambiguous)
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW
    assert transport.submit_entries == 1
    with pytest.raises(HandoffGateRefused, match="not registered"):
        transport.send_with_saved_capability()
    assert transport.physical_calls == 0


def test_forged_copied_reused_stale_and_misbound_permits_are_refused() -> None:
    registry = _PermitRegistry()
    authority = _authority()
    prepared = _prepared()
    key = effect_key(authority.investigation_id, prepared)
    permit = registry.issue(uuid4(), authority, prepared, key)

    forged = object.__new__(ConsumedEffectPermit)
    assert not registry.burn_and_validate(forged, authority, prepared, key)
    copied = copy.copy(permit)
    assert copied is not permit
    assert not registry.burn_and_validate(copied, authority, prepared, key)

    other_authority = ExternalEffectAuthority(
        investigation_id=authority.investigation_id,
        invocation_id=uuid4(),
        operation_key=authority.operation_key,
        reservation_amounts=authority.reservation_amounts,
        expected_envelope_version=authority.expected_envelope_version,
        approval_decision_id=authority.approval_decision_id,
        invocation_arguments_sha256="b" * 64,
    )
    assert not registry.burn_and_validate(permit, other_authority, prepared, key)
    other_prepared = _prepared(wire=_wire(target=b"/mutated"))
    assert not registry.burn_and_validate(permit, authority, other_prepared, key)
    assert registry.burn_and_validate(permit, authority, prepared, key)
    assert not registry.burn_and_validate(permit, authority, prepared, key)


@pytest.mark.asyncio
async def test_prepared_byte_mutation_is_refused_before_consume_or_send() -> None:
    store = FakeStore()
    transport = FakeTransport()
    prepared = _prepared()
    object.__setattr__(prepared, "application_request", _wire(target=b"/changed"))
    outcome = await _coordinator(store).send_one(_authority(), prepared, transport)
    assert isinstance(outcome, Refused)
    assert outcome.reason == "invalid_prepared_effect"
    assert store.events == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_handoff_then_transport_exception_is_ambiguous_and_never_retried() -> None:
    store = FakeStore()
    transport = FakeTransport(response=RuntimeError("remote accepted then client failed"))
    coordinator = _coordinator(store)
    outcome = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert store.sealed == [("needs_review", "transport_outcome_unknown")]
    assert len(transport.calls) == 1
    replay = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(replay, Ambiguous)
    assert len(transport.calls) == 1
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_transport_callback_exception_is_ambiguous_and_never_retried() -> None:
    store = FakeStore()
    transport = FakeTransport(fail_during_submission=True)
    coordinator = _coordinator(store)
    outcome = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert store.sealed == [("needs_review", "transport_outcome_unknown")]
    assert transport.submit_entries == 1
    assert transport.calls == []
    replay = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(replay, Ambiguous)
    assert transport.submit_entries == 1


@pytest.mark.asyncio
async def test_expired_permit_is_refused_before_transport_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    transport = FakeTransport()
    module = importlib.import_module("cti_app.application.external_effects")
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks, 12.0)))
    coordinator = _coordinator(store, permit_ttl_seconds=1.0)
    outcome = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, DefinitelyNotSubmitted)
    assert transport.submit_entries == 0
    assert transport.calls == []


@pytest.mark.asyncio
async def test_permit_expiring_while_handoff_is_paused_never_reaches_physical_send() -> None:
    class SideEffectingPreparationTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.preparation_calls = 0

        def prepare_submission(self, request: FrozenWireRequest) -> None:
            self.preparation_calls += 1
            self.calls.append(request)

    store = FakeStore()
    store.handoff_delay = 0.15
    transport = SideEffectingPreparationTransport()
    outcome = await _coordinator(store, permit_ttl_seconds=0.1).send_one(
        _authority(), _prepared(), transport
    )
    assert isinstance(outcome, Ambiguous)
    assert transport.submit_entries == 0
    assert transport.preparation_calls == 0
    assert transport.calls == []
    assert store.gate_entries == 1
    assert store.handoff_nonce is not None
    assert store.sealed == [("needs_review", "transport_outcome_unknown")]


@pytest.mark.asyncio
async def test_pretransport_seal_prevents_stale_replay_from_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    authority, prepared = _authority(), _prepared()
    transport = FakeTransport()
    module = importlib.import_module("cti_app.application.external_effects")
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks, 12.0)))
    coordinator = _coordinator(store, permit_ttl_seconds=1.0)
    sealed = await coordinator.send_one(authority, prepared, transport)
    assert isinstance(sealed, DefinitelyNotSubmitted)
    assert transport.physical_calls == 0
    replay = await coordinator.send_one(authority, prepared, transport)
    assert isinstance(replay, DefinitelyNotSubmitted)
    assert transport.physical_calls == 0
    assert store.claim_state is EffectClaimState.DEFINITIVELY_NOT_SUBMITTED


@pytest.mark.asyncio
async def test_handoff_refusal_with_existing_marker_stays_ambiguous() -> None:
    store = FakeStore()
    store.handoff_nonce = uuid4()
    transport = FakeTransport()

    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)

    assert isinstance(outcome, Ambiguous)
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW
    assert store.events == ["commit", "gate"]
    assert len(store.no_handoff_proofs) == 1
    assert not any(kind == "not_submitted" for kind, _reason in store.sealed)
    assert transport.submit_entries == 0
    assert transport.physical_calls == 0


@pytest.mark.asyncio
async def test_gate_refuses_request_substitution_and_second_submission() -> None:
    store = FakeStore()
    coordinator = _coordinator(store)
    substituted_transport = FakeTransport(substitute_request=True)
    substituted = await coordinator.send_one(_authority(), _prepared(), substituted_transport)
    assert isinstance(substituted, Ambiguous)
    assert substituted_transport.physical_calls == 0
    assert substituted_transport.submit_entries == 1

    retry_store = FakeStore()
    retry_transport = FakeTransport(retry_inside_transport=True)
    retry = await _coordinator(retry_store).send_one(_authority(), _prepared(), retry_transport)
    assert isinstance(retry, Ambiguous)
    assert retry_transport.physical_calls == 1
    assert retry_transport.submit_entries == 1


@pytest.mark.asyncio
async def test_physical_submit_failure_is_never_classified_not_submitted() -> None:
    store = FakeStore()
    transport = FakeTransport(response=TimeoutError("response outcome unknown"))
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert len(transport.calls) == 1
    assert store.sealed == [("needs_review", "transport_outcome_unknown")]
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_permit_aware_transport_refuses_direct_call_without_capability() -> None:
    transport = FakeTransport()
    with pytest.raises(HandoffGateRefused, match="capability is required"):
        await transport.submit_once(_wire())
    with pytest.raises(HandoffGateRefused, match="capability is required"):
        await transport.submit_once(
            _wire(),
            lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        )
    assert transport.physical_calls == 0


@pytest.mark.asyncio
async def test_capability_rechecks_expiry_at_physical_send() -> None:
    store = FakeStore()
    transport = FakeTransport(send_delay_seconds=0.05)
    outcome = await _coordinator(store, permit_ttl_seconds=0.02).send_one(
        _authority(), _prepared(), transport
    )
    assert isinstance(outcome, Ambiguous)
    assert transport.submit_entries == 1
    assert transport.physical_calls == 0
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_transport_byte_substitution_is_refused_at_physical_send() -> None:
    store = FakeStore()
    transport = FakeTransport(substitute_request=True)
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert transport.physical_calls == 0
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_mutated_noncredential_bytes_fail_exact_send_time_digest() -> None:
    class MutatingTransport:
        def __init__(self) -> None:
            self.physical_calls = 0

        async def submit_once(
            self, request: FrozenWireRequest, capability: PhysicalSendCapability
        ) -> TransportResponse:
            object.__setattr__(request, "body", b"substituted body")
            capability(request)
            self.physical_calls += 1
            return TransportResponse()

    store = FakeStore()
    transport = MutatingTransport()
    prepared = _prepared(wire=_wire(headers=((b"Authorization", b"Bearer ephemeral"),)))
    outcome = await _coordinator(store).send_one(_authority(), prepared, transport)
    assert isinstance(outcome, Ambiguous)
    assert transport.physical_calls == 0
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_changed_unallowlisted_header_value_is_refused_at_send() -> None:
    class MutatingHeaderTransport:
        def __init__(self) -> None:
            self.physical_calls = 0

        async def submit_once(
            self, request: FrozenWireRequest, capability: PhysicalSendCapability
        ) -> TransportResponse:
            object.__setattr__(request, "headers", ((b"Api-Key", b"substituted"),))
            capability(request)
            self.physical_calls += 1
            return TransportResponse()

    original = _wire(headers=((b"Api-Key", b"original"),))
    prepared = _prepared(wire=original)
    store = FakeStore()
    transport = MutatingHeaderTransport()
    outcome = await _coordinator(store).send_one(_authority(), prepared, transport)
    assert isinstance(outcome, Ambiguous)
    assert transport.physical_calls == 0
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_legacy_no_submit_callback_cannot_authorize_retry() -> None:
    class LegacyCallbackTransport:
        def __init__(self) -> None:
            self.physical_calls = 0
            self.legacy_callback_unavailable = False

        async def submit_once(
            self, request: FrozenWireRequest, capability: PhysicalSendCapability
        ) -> TransportResponse:
            assert not hasattr(capability, "report_pre_submission_failure")
            capability(request)
            self.physical_calls += 1
            try:
                callback_name = "report_pre_submission_failure"
                getattr(capability, callback_name)(httpx.ConnectError("fabricated no-submit proof"))
            except AttributeError:
                self.legacy_callback_unavailable = True
            raise httpx.ConnectError("transport failed after the handoff marker")

    store = FakeStore()
    transport = LegacyCallbackTransport()
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert transport.physical_calls == 1
    assert transport.legacy_callback_unavailable
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_transport_retry_cannot_obtain_second_physical_send() -> None:
    store = FakeStore()
    transport = FakeTransport(retry_inside_transport=True)
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert transport.physical_calls == 1
    assert len(transport.calls) == 1
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_transport_refuses_client_with_retries_enabled() -> None:
    store = FakeStore()
    transport = FakeTransport(client_retries_enabled=True)
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert transport.physical_calls == 0
    assert store.claim_state is EffectClaimState.NEEDS_REVIEW


def test_no_handoff_proofs_are_opaque_bound_and_one_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = uuid4()
    other_claim_id = uuid4()
    module = importlib.import_module("cti_app.application.external_effects")
    ticks = iter((10.0, 12.0, 20.0, 22.0))
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: next(ticks, 22.0)))

    def capture_from_pretransport_failure(
        target_claim: UUID,
    ) -> tuple[FakeStore, object, ExternalEffectCoordinator]:
        base_store = FakeStore(ConsumeCommitted(target_claim))
        captured: list[object] = []

        class _ProofCaptureStore:
            def __getattr__(self, name: str) -> Any:
                return getattr(base_store, name)

            async def definitely_not_submitted(
                self, claim: UUID, *, proof: object, reason_code: str
            ) -> bool:
                captured.append(proof)
                return False

            async def needs_review(self, claim: UUID, *, reason_code: str) -> bool:
                return True

        coordinator = ExternalEffectCoordinator(  # type: ignore[arg-type]
            _ProofCaptureStore(), permit_ttl_seconds=1.0
        )
        outcome = asyncio.run(coordinator.send_one(_authority(), _prepared(), FakeTransport()))
        assert isinstance(outcome, Ambiguous)
        assert len(captured) == 1
        return base_store, captured[0], coordinator

    store, proof, coordinator = capture_from_pretransport_failure(claim_id)
    _foreign_store, foreign_proof, _foreign_coordinator = capture_from_pretransport_failure(
        other_claim_id
    )
    assert "_proof_authority" not in dir(coordinator)
    assert "_seal_zero_handoff" not in dir(coordinator)
    assert "_permits" not in dir(coordinator)
    assert not any("issue_no_handoff_proof" in name or "arm_claim" in name for name in dir(module))
    assert not any(
        "proof_authority" in name or "issue_no_handoff_proof" in name for name in vars(coordinator)
    )
    assert not any("issue_no_handoff_proof" in name or "arm_claim" in name for name in vars(module))

    copied = copy.copy(proof)
    forged = object()

    async def attempt(target: UUID, token: object) -> bool:
        return await store.definitely_not_submitted(target, proof=token, reason_code="test_no_send")

    assert not asyncio.run(attempt(claim_id, forged))
    assert not asyncio.run(attempt(claim_id, copied))
    assert not asyncio.run(attempt(claim_id, foreign_proof))
    assert not asyncio.run(attempt(other_claim_id, proof))
    assert store.claim_state is EffectClaimState.CONSUMED
    assert asyncio.run(attempt(claim_id, proof))
    assert not asyncio.run(attempt(claim_id, proof))
    assert store.sealed == [("not_submitted", "test_no_send")]


@pytest.mark.asyncio
async def test_post_handoff_timeout_is_ambiguous_and_never_retried() -> None:
    store = FakeStore()
    transport = FakeTransport(response=TimeoutError("response lost"))
    coordinator = _coordinator(store)
    first = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(first, Ambiguous)
    assert store.sealed == [("needs_review", "transport_outcome_unknown")]
    second = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(second, Ambiguous)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_completion_cas_failure_becomes_ambiguous() -> None:
    store = FakeStore()
    store.complete_result = False
    transport = FakeTransport()
    outcome = await _coordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert store.sealed == [("needs_review", "outcome_persist_failed")]
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_completed_effect_requires_family_reuse_policy_and_records_zero_budget_lineage() -> (
    None
):
    refs = (EvidenceReference("vt_observation", "obs-1", "v1", "c" * 64),)
    claim = ExternalEffectClaim(
        id=uuid4(),
        investigation_id=_INVESTIGATION_ID,
        invocation_id=uuid4(),
        effect_key=effect_key(_INVESTIGATION_ID, _prepared()),
        family="vt.file_report",
        provider="virustotal",
        provider_scope="tenant-prod",
        canonical_request_sha256=_prepared().canonical_request_sha256,
        application_request_sha256=_prepared().application_request_sha256,
        envelope_version=4,
        approval_decision_id=_APPROVAL_ID,
        state=EffectClaimState.COMPLETED,
        evidence_refs=refs,
        result_ref="artifact:result-1",
    )
    no_policy_store = FakeStore(ExistingClaim(claim))
    no_policy_transport = FakeTransport()
    denied = await _coordinator(no_policy_store).send_one(
        _authority(), _prepared(), no_policy_transport
    )
    assert isinstance(denied, Refused)
    assert denied.reason == "effect_occurrence_required"
    assert no_policy_transport.calls == []
    assert no_policy_store.refused_reuse_reservations == ["op-1"]

    policy_store = FakeStore(ExistingClaim(claim))
    transport = FakeTransport()
    policy = AllowReuse(claim.invocation_id, refs)
    reused = await _coordinator(policy_store, reuse_policies={"vt.file_report": policy}).send_one(
        _authority(amounts={"vt_units": 2, "tool_seconds": 5}), _prepared(), transport
    )
    assert reused.__class__.__name__ == "Reused"
    assert reused.observation_refs == refs  # type: ignore[union-attr]
    assert dict(reused.budget_delta) == {"tool_seconds": 0, "vt_units": 0}  # type: ignore[union-attr]
    assert policy_store.reuse_calls == [policy.decision]
    assert transport.calls == []


@pytest.mark.asyncio
async def test_existing_claim_with_changed_request_digest_is_refused() -> None:
    original = _prepared()
    claim = ExternalEffectClaim(
        id=uuid4(),
        investigation_id=_INVESTIGATION_ID,
        invocation_id=uuid4(),
        effect_key=effect_key(_INVESTIGATION_ID, original),
        family=original.family,
        provider=original.provider,
        provider_scope=original.provider_scope,
        canonical_request_sha256=original.canonical_request_sha256,
        application_request_sha256=original.application_request_sha256,
        envelope_version=4,
        approval_decision_id=_APPROVAL_ID,
        state=EffectClaimState.COMPLETED,
    )
    store = FakeStore(ExistingClaim(claim))
    changed_wire = _prepared(wire=_wire(target=b"/different"))
    transport = FakeTransport()
    outcome = await _coordinator(store).send_one(_authority(), changed_wire, transport)
    assert isinstance(outcome, Refused)
    assert outcome.reason == "effect_binding_conflict"
    assert transport.calls == []


def test_no_secret_fields_or_signed_urls_are_persistable_or_repr_visible() -> None:
    with pytest.raises(ExternalEffectValidationError):
        _prepared(request={"api_key": "do-not-store"})
    with pytest.raises(ExternalEffectValidationError):
        _prepared(request={"token": "do-not-store"})
    with pytest.raises(ExternalEffectValidationError):
        _prepared(request={"url": "https://objects.example/file?X-Amz-Signature=secret"})
    with pytest.raises(ExternalEffectValidationError):
        EvidenceReference("observation", "obs-1", "https://x.example/?token=secret", "a" * 64)
    secret_wire = _wire(
        target=b"https://objects.example/file?sig=ephemeral",
        headers=((b"authorization", b"Bearer ephemeral-secret"),),
        body=b"ephemeral-body",
    )
    assert "ephemeral-secret" not in repr(secret_wire)
    assert "ephemeral-body" not in repr(secret_wire)
    assert "sig=ephemeral" not in repr(secret_wire)


@pytest.mark.parametrize(
    "url",
    [
        "https://host/path?X-Api-Key=secret",
        "https://host/path?x_api_key=secret",
        "https://host/path?client_secret=secret",
        "https://host/path?access_token=secret",
        "https://host/path?signature=secret",
        "https://host/path?token=secret",
        "https://host/path?%78_api_key=secret",
    ],
)
def test_credential_query_aliases_are_rejected_from_persisted_urls(url: str) -> None:
    with pytest.raises(ExternalEffectValidationError):
        _prepared(request={"url": url})
    with pytest.raises(ExternalEffectValidationError):
        PreparedExternalRequest.build(
            family="vt.file_report",
            provider="virustotal",
            provider_scope="tenant-prod",
            normalizer_version="vt.file-report.v1",
            canonical_request={"hash": "abc"},
            request_ref=url,
            application_request=_wire(),
        )
    with pytest.raises(ExternalEffectValidationError):
        EvidenceReference("observation", url, "v1", "a" * 64)


@pytest.mark.parametrize(
    "url",
    [
        "https://host/path#access_token=secret",
        "https://host/path#api_key=secret",
        "https://host/path#client_secret=secret",
        "https://host/path#signature=secret",
        "https://host/path#token=secret",
        "https://host/path#X-Amz-Signature=secret",
        "https://host/path#x-amz-credential=secret",
        "https://host/path#%78_api_key=secret",
    ],
)
def test_credential_fragment_aliases_are_rejected_before_persistence(url: str) -> None:
    with pytest.raises(ExternalEffectValidationError) as canonical_error:
        _prepared(request={"url": url})
    assert "secret" not in str(canonical_error.value)
    with pytest.raises(ExternalEffectValidationError) as reference_error:
        PreparedExternalRequest.build(
            family="vt.file_report",
            provider="virustotal",
            provider_scope="tenant-prod",
            normalizer_version="vt.file-report.v1",
            canonical_request={"hash": "abc"},
            request_ref=url,
            application_request=_wire(),
        )
    assert "secret" not in str(reference_error.value)
    with pytest.raises(ExternalEffectValidationError) as evidence_error:
        EvidenceReference("observation", url, "v1", "a" * 64)
    assert "secret" not in str(evidence_error.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://host/path?id_token=credential-probe",
        "https://host/path?ID-TOKEN=credential-probe",
        "https://host/path?refresh_token=credential-probe",
        "https://host/path?Refresh-Token=credential-probe",
        "https://host/path#id_token=credential-probe",
        "https://host/path#ID-TOKEN=credential-probe",
        "https://host/path#refresh_token=credential-probe",
        "https://host/path#Refresh-Token=credential-probe",
        "https://host/path?private_key=credential-probe",
        "https://host/path?PRIVATE-KEY=credential-probe",
        "https://host/path#private_key=credential-probe",
        "https://host/path#PRIVATE-KEY=credential-probe",
    ],
)
def test_oauth_token_aliases_never_reach_persisted_refs_or_errors(url: str) -> None:
    failures = []
    with pytest.raises(ExternalEffectValidationError) as canonical_error:
        _prepared(request={"url": url})
    failures.append(canonical_error.value)
    with pytest.raises(ExternalEffectValidationError) as request_ref_error:
        PreparedExternalRequest.build(
            family="vt.file_report",
            provider="virustotal",
            provider_scope="tenant-prod",
            normalizer_version="vt.file-report.v1",
            canonical_request={"hash": "abc"},
            request_ref=url,
            application_request=_wire(),
        )
    failures.append(request_ref_error.value)
    with pytest.raises(ExternalEffectValidationError) as evidence_error:
        EvidenceReference("observation", url, "v1", "a" * 64)
    failures.append(evidence_error.value)

    for error in failures:
        assert "credential-probe" not in str(error)
        assert url not in str(error)
        assert "credential-probe" not in repr(error)
        assert url not in repr(error)


@pytest.mark.asyncio
async def test_osint_policy_authority_defaults_to_deny_without_guard() -> None:
    async def forbidden_session_factory() -> object:
        raise AssertionError("default deny must happen before database I/O")

    store = SqlAlchemyExternalEffectClaimStore(
        forbidden_session_factory,  # type: ignore[arg-type]
    )
    outcome = await store.consume(
        _authority(invocation_id=None, policy=True),
        _prepared(),
        effect_key=effect_key(_INVESTIGATION_ID, _prepared()),
        occurrence_digest=occurrence_sha256(None),
    )
    assert outcome == ConsumeRefused("osint_policy_guard_unavailable")


@pytest.mark.asyncio
async def test_osint_policy_guard_locks_before_envelope() -> None:
    events: list[str] = []

    class _NoopContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object):
            return None

        def begin(self):
            return _NoopContext()

        async def scalar(self, *args: object, **kwargs: object):
            return None

    class _Guard:
        async def lock_for_consume(self, session, authority):
            events.append("policy")
            return True

        async def authorize(self, context):
            return True

        async def authorize_source_disclosure(self, context):
            return True

    class _OrderingStore(SqlAlchemyExternalEffectClaimStore):
        async def _lock_authority(self, *args, **kwargs):
            events.append("envelope")
            raise _ConsumeDenied("stop after order probe")

    prepared = _prepared()
    store = _OrderingStore(
        lambda: _Session(),  # type: ignore[arg-type]
        policy_guard=_Guard(),
        family_bindings={
            prepared.family: ExternalEffectFamilyBinding(
                provider=prepared.provider,
                provider_scopes=frozenset({prepared.provider_scope}),
                normalizer_versions=frozenset({prepared.normalizer_version}),
                invocation_tool_binding=(prepared.family, "v1"),
            )
        },
    )
    authority = _authority(invocation_id=None, policy=True)
    result = await store.consume(
        authority,
        prepared,
        effect_key=effect_key(authority.investigation_id, prepared),
        occurrence_digest=occurrence_sha256(prepared.occurrence_id),
    )
    assert result == ConsumeRefused("stop after order probe")
    assert events == ["policy", "envelope"]


def test_authority_requires_explicit_osint_policy_bindings() -> None:
    with pytest.raises(ExternalEffectValidationError):
        ExternalEffectAuthority(
            investigation_id=_INVESTIGATION_ID,
            invocation_id=None,
            operation_key="op-1",
            reservation_amounts={"vt_units": 1},
            expected_envelope_version=4,
            approval_decision_id=_APPROVAL_ID,
            invocation_arguments_sha256=None,
            authority_kind=ExternalEffectAuthorityKind.OSINT_POLICY,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["omitted", "malformed", "mismatched"])
async def test_source_dependent_family_rejects_untrusted_source_refs(case: str) -> None:
    expected_id = UUID("89c70553-4511-40a1-8f30-14f9e3fc4ea8")
    other_id = UUID("5a98ad22-ec4f-4cb4-a6b2-f75b49d4c0bd")
    actual_refs = {
        "omitted": (),
        "malformed": (EvidenceReference("sample", "not-a-uuid", "v1", "c" * 64),),
        "mismatched": (EvidenceReference("sample", str(other_id), "v1", "c" * 64),),
    }[case]
    prepared = _prepared(evidence_refs=actual_refs)
    authority = _authority()

    class _NoopContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object):
            return None

        def begin(self):
            return _NoopContext()

        async def scalar(self, *args: object, **kwargs: object):
            return None

    class _SourceCheckStore(SqlAlchemyExternalEffectClaimStore):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

        async def _lock_authority(self, *args, **kwargs):
            invocation = type(
                "Invocation",
                (),
                {"normalized_parameters": {"sample_id": str(expected_id)}},
            )()
            return (object(), object(), invocation, object(), [])

        async def _check_authority(self, *args, **kwargs):
            return None

    store = _SourceCheckStore(
        lambda: _Session(),  # type: ignore[arg-type]
        family_bindings={
            prepared.family: ExternalEffectFamilyBinding(
                provider=prepared.provider,
                provider_scopes=frozenset({prepared.provider_scope}),
                normalizer_versions=frozenset({prepared.normalizer_version}),
                invocation_tool_binding=(prepared.family, "v1"),
                requires_source_refs=True,
                source_reference_resolver=lambda parameters: (
                    EvidenceReference("sample", parameters["sample_id"], "v1", "c" * 64),
                ),
            )
        },
    )
    transport = FakeTransport()
    direct_result = await store.consume(
        authority,
        prepared,
        effect_key=effect_key(authority.investigation_id, prepared),
        occurrence_digest=occurrence_sha256(prepared.occurrence_id),
    )
    assert isinstance(direct_result, ConsumeRefused)
    result = await _coordinator(store).send_one(authority, prepared, transport)
    assert isinstance(result, Refused)
    assert result.reason in {"malformed_source_reference", "source_references_mismatch"}
    assert transport.submit_entries == 0
    assert transport.calls == []


@pytest.mark.parametrize(
    ("request_sandbox", "expected_sandbox", "denied"),
    [
        ("sandbox-1", "sandbox-1", False),
        ("sandbox-2", "sandbox-1", True),
        ("sandbox%3A1", "sandbox:1", False),
    ],
)
def test_vt_pcap_source_hash_is_bound_to_persisted_request_target(
    request_sandbox: str, expected_sandbox: str, denied: bool
) -> None:
    file_hash = "a" * 64
    source_reference = EvidenceReference("sample_hash", file_hash, "sample-hash.v1", file_hash)
    prepared = _prepared(
        family="vt.behaviour_pcap",
        request={"path": f"/api/v3/file_behaviours/{request_sandbox}/pcap"},
        evidence_refs=(source_reference,),
    )
    invocation = SimpleNamespace(
        normalized_parameters={"file_hash": file_hash, "sandbox_id": expected_sandbox}
    )
    binding = ExternalEffectFamilyBinding(
        provider=prepared.provider,
        provider_scopes=frozenset({prepared.provider_scope}),
        normalizer_versions=frozenset({prepared.normalizer_version}),
        invocation_tool_binding=("vt.behaviour_pcap", "1"),
        requires_source_refs=True,
        source_reference_resolver=lambda _parameters: (source_reference,),
    )
    store = SqlAlchemyExternalEffectClaimStore(
        lambda: None,  # type: ignore[arg-type]
        family_bindings={prepared.family: binding},
    )

    if denied:
        with pytest.raises(_ConsumeDenied, match="vt_source_hash_request_mismatch"):
            store._validate_family_source_refs(binding, invocation, prepared)
    else:
        store._validate_family_source_refs(binding, invocation, prepared)


@pytest.mark.parametrize("drift", ["missing_default", "extra_index"])
def test_migration_preflight_rejects_default_and_complete_index_drift(
    monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    migration = importlib.import_module("migrations.versions.0004_external_effect_claims")
    expected_tables = migration._expected_tables()

    class _Result:
        def scalars(self):
            return ["external_effect_claim", "external_effect_use"]

    class _Bind:
        ddl_calls = 0

        def execute(self, statement, parameters=None):
            return _Result()

    class _Inspector:
        def get_table_names(self):
            return ["external_effect_claim", "external_effect_use"]

        def _table(self, name: str):
            return next(table for table in expected_tables if table.name == name)

        def get_columns(self, name: str):
            rows = []
            for column in self._table(name).columns:
                default = (
                    str(column.server_default.arg) if column.server_default is not None else None
                )
                if (
                    drift == "missing_default"
                    and name == "external_effect_claim"
                    and column.name == "evidence_refs"
                ):
                    default = None
                rows.append(
                    {
                        "name": column.name,
                        "type": column.type.compile(dialect=postgresql.dialect()),
                        "nullable": column.nullable,
                        "default": default,
                    }
                )
            return rows

        def get_pk_constraint(self, name: str):
            constraint = next(
                item
                for item in self._table(name).constraints
                if item.__class__.__name__ == "PrimaryKeyConstraint"
            )
            return {
                "name": constraint.name,
                "constrained_columns": [column.name for column in constraint.columns],
            }

        def get_unique_constraints(self, name: str):
            return [
                {"name": item.name, "column_names": [column.name for column in item.columns]}
                for item in self._table(name).constraints
                if item.__class__.__name__ == "UniqueConstraint"
            ]

        def get_check_constraints(self, name: str):
            return [
                {"name": item.name, "sqltext": str(item.sqltext)}
                for item in self._table(name).constraints
                if item.__class__.__name__ == "CheckConstraint"
            ]

        def get_foreign_keys(self, name: str):
            rows = []
            for item in self._table(name).constraints:
                if item.__class__.__name__ != "ForeignKeyConstraint":
                    continue
                elements = tuple(item.elements)
                rows.append(
                    {
                        "name": item.name,
                        "constrained_columns": [column.name for column in item.columns],
                        "referred_table": elements[0].column.table.name,
                        "referred_columns": [element.column.name for element in elements],
                        "options": {"ondelete": item.ondelete},
                    }
                )
            return rows

        def get_indexes(self, name: str):
            rows = [
                {
                    "name": item.name,
                    "column_names": [column.name for column in item.columns],
                    "unique": item.unique,
                }
                for item in self._table(name).indexes
            ]
            if drift == "extra_index" and name == "external_effect_claim":
                rows.append(
                    {
                        "name": "ix_external_effect_claim_unexpected",
                        "column_names": ["id"],
                        "unique": False,
                    }
                )
            return rows

    inspector = _Inspector()
    monkeypatch.setattr(migration, "inspect", lambda _bind: inspector)
    bind = _Bind()
    with pytest.raises(RuntimeError, match="drift"):
        migration._preflight(bind)
    assert bind.ddl_calls == 0


@pytest.mark.parametrize(
    "drift",
    [None, "missing_dispatch_check", "partial_schema", "weaker_terminal_index_predicate"],
)
def test_acquisition_dispatch_migration_preflight_checks_complete_shape(
    monkeypatch: pytest.MonkeyPatch, drift: str | None
) -> None:
    migration = importlib.import_module("migrations.versions.0005_sample_acquisition_dispatch")
    tables = (
        migration.SampleAcquisitionDispatchAttemptRow.__table__,
        migration.ExternalEffectAcquisitionDispatchRow.__table__,
    )
    terminal = importlib.import_module(
        "cti_app.infrastructure.database.models.production"
    ).SampleAcquisitionAttemptRow.__table__

    class _Inspector:
        def get_table_names(self):
            if drift == "partial_schema":
                return [tables[0].name, terminal.name]
            return [table.name for table in (*tables, terminal)]

        def _table(self, name: str):
            return next(table for table in (*tables, terminal) if table.name == name)

        def get_columns(self, name: str):
            return [
                {
                    "name": column.name,
                    "type": column.type.compile(dialect=postgresql.dialect()),
                    "nullable": column.nullable,
                    "default": (
                        str(column.server_default.arg)
                        if column.server_default is not None
                        else None
                    ),
                }
                for column in self._table(name).columns
            ]

        def get_pk_constraint(self, name: str):
            constraint = next(
                item
                for item in self._table(name).constraints
                if item.__class__.__name__ == "PrimaryKeyConstraint"
            )
            return {
                "name": constraint.name,
                "constrained_columns": [column.name for column in constraint.columns],
            }

        def get_unique_constraints(self, name: str):
            return [
                {"name": item.name, "column_names": [column.name for column in item.columns]}
                for item in self._table(name).constraints
                if item.__class__.__name__ == "UniqueConstraint"
            ]

        def get_check_constraints(self, name: str):
            checks = [
                {"name": item.name, "sqltext": str(item.sqltext)}
                for item in self._table(name).constraints
                if item.__class__.__name__ == "CheckConstraint"
            ]
            if drift == "missing_dispatch_check" and name == tables[0].name:
                return checks[:-1]
            return checks

        def get_foreign_keys(self, name: str):
            result = []
            for item in self._table(name).constraints:
                if item.__class__.__name__ != "ForeignKeyConstraint":
                    continue
                elements = tuple(item.elements)
                result.append(
                    {
                        "name": item.name,
                        "constrained_columns": [column.name for column in item.columns],
                        "referred_table": elements[0].column.table.name,
                        "referred_columns": [element.column.name for element in elements],
                        "options": {"ondelete": item.ondelete},
                    }
                )
            return result

        def get_indexes(self, name: str):
            rows = []
            for item in self._table(name).indexes:
                row = {
                    "name": item.name,
                    "column_names": [column.name for column in item.columns],
                    "unique": item.unique,
                    "duplicates_constraint": None,
                }
                if item.name == migration._INDEX:
                    predicate = "dispatch_attempt_id IS NOT NULL"
                    if drift == "weaker_terminal_index_predicate":
                        predicate += (
                            " AND dispatch_attempt_id <> '00000000-0000-0000-0000-000000000000'"
                        )
                    row["dialect_options"] = {"postgresql_where": predicate}
                rows.append(row)
            return rows

    inspector = _Inspector()
    monkeypatch.setattr(migration, "inspect", lambda _bind: inspector)
    if drift is None:
        assert migration._preflight(object()) is True
    elif drift == "partial_schema":
        with pytest.raises(RuntimeError, match="partial schema"):
            migration._preflight(object())
    elif drift == "missing_dispatch_check":
        with pytest.raises(RuntimeError, match="constraints"):
            migration._preflight(object())
    else:
        with pytest.raises(RuntimeError, match="terminal index drift"):
            migration._preflight(object())


def test_acquisition_dispatch_migration_refuses_to_drop_persisted_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module("migrations.versions.0005_sample_acquisition_dispatch")

    class _Result:
        def scalar_one(self) -> bool:
            return True

    class _Bind:
        statements: list[str]

        def __init__(self) -> None:
            self.statements = []

        def execute(self, statement: Any) -> _Result:
            self.statements.append(str(statement))
            return _Result()

    class _Operations:
        def __init__(self) -> None:
            self.bind = _Bind()

        def get_bind(self) -> _Bind:
            return self.bind

    operations = _Operations()
    monkeypatch.setattr(migration, "op", operations)

    with pytest.raises(RuntimeError, match="refusing to drop sample acquisition dispatch lineage"):
        migration.downgrade()

    assert operations.bind.statements[0].startswith("LOCK TABLE")
    assert "effect_acquisition_dispatch_links" in operations.bind.statements[0]
    assert "SELECT EXISTS" in operations.bind.statements[1]

from __future__ import annotations

import copy
import importlib
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from cti_app.application.external_effects import (
    Ambiguous,
    Completed,
    ConsumeCommitted,
    ConsumeRefused,
    DefinitelyNotSubmitted,
    ExistingClaim,
    ExternalEffectClaim,
    ExternalEffectCoordinator,
    Refused,
    RequestHandoff,
    ReuseDecision,
    TransportResponse,
    _PermitRegistry,
)
from cti_app.domain.external_effects import (
    ConsumedEffectPermit,
    EffectClaimState,
    EvidenceReference,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
    ExternalEffectValidationError,
    FrozenWireRequest,
    PreparedExternalRequest,
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
        self.sealed: list[tuple[str, str]] = []
        self.reuse_id: UUID | None = uuid4()
        self.reuse_calls: list[ReuseDecision] = []
        self._first_consume = True

    async def consume(
        self,
        authority: ExternalEffectAuthority,
        prepared: PreparedExternalRequest,
        **kwargs: object,
    ) -> object:
        self.events.append("commit")
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
                    state=EffectClaimState.NEEDS_REVIEW,
                )
            )
        self._first_consume = False
        return self.result

    async def complete(self, *args: object, **kwargs: object) -> bool:
        self.events.append("complete")
        return self.complete_result

    async def definitely_not_submitted(
        self, claim_id: UUID, *, proof: object, reason_code: str
    ) -> bool:
        assert getattr(proof, "claim_id", None) == claim_id
        assert getattr(proof, "handoff_count", None) == 0
        self.sealed.append(("not_submitted", reason_code))
        return True

    async def needs_review(self, claim_id: UUID, *, reason_code: str) -> bool:
        self.sealed.append(("needs_review", reason_code))
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


class FakeTransport:
    def __init__(
        self,
        events: list[str] | None = None,
        response: object | Exception | None = None,
        *,
        fail_before_handoff: bool = False,
    ) -> None:
        self.calls: list[tuple[FrozenWireRequest, ConsumedEffectPermit]] = []
        self.send_entries = 0
        self.events = events
        self.response = response or TransportResponse(result_ref="artifact:result-1")
        self.fail_before_handoff = fail_before_handoff

    async def send(
        self,
        request: FrozenWireRequest,
        *,
        permit: ConsumedEffectPermit,
        handoff: RequestHandoff,
    ) -> TransportResponse:
        self.send_entries += 1
        if self.events is not None:
            assert self.events[-1] == "commit"
            self.events.append("send")
        if self.fail_before_handoff:
            raise RuntimeError("local transport setup failed")

        async def physical_send() -> TransportResponse:
            self.calls.append((request, permit))
            if isinstance(self.response, Exception):
                raise self.response
            return self.response  # type: ignore[return-value]

        return await handoff(physical_send)


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
        _wire(headers=((b"x-repeat", b"one"), (b"x-repeat", b"two")))
    ) != application_request_sha256(_wire(headers=((b"x-repeat", b"two"), (b"x-repeat", b"one"))))


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
    outcome = await ExternalEffectCoordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Refused)
    assert outcome.reason == "provider_not_allowed"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_committed_consume_mints_one_permit_and_calls_transport_once() -> None:
    store = FakeStore()
    transport = FakeTransport(store.events)
    outcome = await ExternalEffectCoordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Completed)
    assert store.events == ["commit", "send", "complete"]
    assert len(transport.calls) == 1
    request, permit = transport.calls[0]
    assert request == _prepared().application_request
    assert permit.claim_id == store.result.claim_id  # type: ignore[union-attr]


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
    outcome = await ExternalEffectCoordinator(store).send_one(_authority(), prepared, transport)
    assert isinstance(outcome, Refused)
    assert outcome.reason == "invalid_prepared_effect"
    assert store.events == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_handoff_then_transport_exception_is_ambiguous_and_never_retried() -> None:
    store = FakeStore()
    transport = FakeTransport(response=RuntimeError("remote accepted then client failed"))
    coordinator = ExternalEffectCoordinator(store)
    outcome = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, Ambiguous)
    assert store.sealed == [("needs_review", "transport_outcome_unknown")]
    assert len(transport.calls) == 1
    replay = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(replay, Ambiguous)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_zero_handoff_counter_allows_definitive_not_submitted() -> None:
    store = FakeStore()
    transport = FakeTransport(fail_before_handoff=True)
    outcome = await ExternalEffectCoordinator(store).send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, DefinitelyNotSubmitted)
    assert store.sealed == [("not_submitted", "transport_failed_before_handoff")]
    assert transport.send_entries == 1
    assert transport.calls == []


@pytest.mark.asyncio
async def test_expired_permit_is_refused_before_transport_entry() -> None:
    store = FakeStore()
    transport = FakeTransport()
    coordinator = ExternalEffectCoordinator(store)
    coordinator._permits._ttl_seconds = -1
    outcome = await coordinator.send_one(_authority(), _prepared(), transport)
    assert isinstance(outcome, DefinitelyNotSubmitted)
    assert transport.send_entries == 0
    assert transport.calls == []


@pytest.mark.asyncio
async def test_post_handoff_timeout_is_ambiguous_and_never_retried() -> None:
    store = FakeStore()
    transport = FakeTransport(response=TimeoutError("response lost"))
    coordinator = ExternalEffectCoordinator(store)
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
    outcome = await ExternalEffectCoordinator(store).send_one(_authority(), _prepared(), transport)
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
    store = FakeStore(ExistingClaim(claim))
    no_policy_transport = FakeTransport()
    denied = await ExternalEffectCoordinator(store).send_one(
        _authority(), _prepared(), no_policy_transport
    )
    assert isinstance(denied, Refused)
    assert denied.reason == "effect_occurrence_required"
    assert no_policy_transport.calls == []

    transport = FakeTransport()
    policy = AllowReuse(claim.invocation_id, refs)
    reused = await ExternalEffectCoordinator(
        store, reuse_policies={"vt.file_report": policy}
    ).send_one(_authority(amounts={"vt_units": 2, "tool_seconds": 5}), _prepared(), transport)
    assert reused.__class__.__name__ == "Reused"
    assert reused.observation_refs == refs  # type: ignore[union-attr]
    assert dict(reused.budget_delta) == {"tool_seconds": 0, "vt_units": 0}  # type: ignore[union-attr]
    assert store.reuse_calls == [policy.decision]
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
    outcome = await ExternalEffectCoordinator(store).send_one(_authority(), changed_wire, transport)
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
    result = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
    assert isinstance(result, Refused)
    assert result.reason in {"malformed_source_reference", "source_references_mismatch"}
    assert transport.send_entries == 0
    assert transport.calls == []


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

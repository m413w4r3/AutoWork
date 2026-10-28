from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.investigation import router as investigation_router
from cti_app.api.osint_policy import router as policy_router
from cti_app.application.analyst_handoff import analyst_handoff_policy_from_sources
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.investigation_governance import (
    InvestigationActor,
    SampleQualificationPermissionError,
    SampleQualificationService,
    _copy_budget,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.investigation_tools import ToolEffect
from cti_app.application.operator_tool_invocation import (
    OperatorToolInvocationError,
    OperatorToolInvocationService,
)
from cti_app.application.osint_open_policy import (
    OsintOpenPolicyService,
    OsintPolicyConflictError,
    OsintPolicyError,
)
from cti_app.config import Settings
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetReservationStatus,
    DisclosureBounds,
    EnvelopeAuthorityKind,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    QualificationActorKind,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
)
from cti_app.domain.osint_open import (
    OsintCapabilities,
    OsintScopeKind,
    intersect_disclosure,
)
from tests.investigation_c2_fakes import FakeDatabase, FakeUow, make_investigation
from tests.test_investigation_service import _Fixture
from tests.test_operator_tool_invocation import (
    _CompositionFactory,
    _IdentityProvider,
    _Uow,
)
from tests.test_operator_tool_invocation import (
    _fixture as operator_fixture,
)
from tests.test_operator_tool_invocation import (
    _service as operator_service,
)


def _capabilities(**overrides: object) -> OsintCapabilities:
    values: dict[str, object] = {
        "allowed_providers": ("bridge", "shodan", "vt"),
        "budget_limits": {
            BudgetDimension.VT_UNITS: 100,
            BudgetDimension.SHODAN_CREDITS: 20,
            BudgetDimension.DOWNLOADS: 10,
            BudgetDimension.BRIDGE_TURNS: 20,
            BudgetDimension.TOOL_SECONDS: 3600,
            BudgetDimension.CONTEXT_BYTES: 1_000_000,
            BudgetDimension.CONTEXT_TOKENS: 100_000,
        },
        "acquire_hits_allowed": True,
        "disclosure": DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        "max_effect_claims_per_investigation": 25,
    }
    overrides = dict(overrides)
    budget_dimension_aliases = {
        "vt_units": BudgetDimension.VT_UNITS,
        "shodan_credits": BudgetDimension.SHODAN_CREDITS,
        "downloads": BudgetDimension.DOWNLOADS,
        "bridge_turns": BudgetDimension.BRIDGE_TURNS,
        "tool_seconds": BudgetDimension.TOOL_SECONDS,
        "context_bytes": BudgetDimension.CONTEXT_BYTES,
        "context_tokens": BudgetDimension.CONTEXT_TOKENS,
    }
    base_limits = values["budget_limits"]
    assert isinstance(base_limits, dict)
    budget_limits = dict(base_limits)
    for name, dimension in budget_dimension_aliases.items():
        if name in overrides:
            budget_limits[dimension] = overrides.pop(name)
    values["budget_limits"] = budget_limits
    values.update(overrides)
    return OsintCapabilities(**values)  # type: ignore[arg-type]


def _settings(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "osint_open_max_vt_units": 100_000,
        "osint_open_max_downloads": 10_000,
        "osint_open_max_bridge_turns": 1_000,
        "osint_open_max_tool_seconds": 86_400,
        "osint_open_max_context_bytes": 128 * 1024 * 1024,
        "osint_open_max_context_tokens": 32_000_000,
        "osint_open_max_shodan_credits": 100_000,
        "osint_open_max_effect_claims": 10_000,
    }
    fields.update(overrides)
    return Settings.model_construct(**fields)


def test_budget_copy_preserves_reserved_external_usage_after_revocation() -> None:
    budget = InvestigationBudget(limits={BudgetDimension.VT_UNITS: 3})
    reservation = budget.reserve(
        "reserved-before-revocation",
        {BudgetDimension.VT_UNITS: 3},
    )

    copied = _copy_budget(budget, limits={BudgetDimension.VT_UNITS: 0})

    assert copied.limits == {BudgetDimension.VT_UNITS: 0}
    assert copied.reservations == (reservation,)
    assert copied.reservations[0].status is BudgetReservationStatus.RESERVED
    assert copied.reserved[BudgetDimension.VT_UNITS] == 3


@pytest.mark.parametrize(
    ("source_tlp", "expected_tlp", "external_allowed"),
    (
        ("clear", TLPLevel.GREEN, True),
        ("GREEN", TLPLevel.GREEN, True),
        ("amber", TLPLevel.AMBER, True),
        ("RED", TLPLevel.RED, False),
        (None, TLPLevel.RED, False),
        ("", TLPLevel.RED, False),
        ("invalid", TLPLevel.RED, False),
    ),
)
def test_disclosure_intersection_uses_most_restrictive_tlp_and_fails_closed(
    source_tlp: str | None,
    expected_tlp: TLPLevel,
    external_allowed: bool,
) -> None:
    result = intersect_disclosure(
        source_tlp,
        False,
        True,
        DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
    )
    assert result.tlp is expected_tlp
    assert result.external_llm_allowed is external_allowed
    if source_tlp in (None, "", "invalid"):
        assert result.do_not_submit is True


def test_disclosure_intersection_preserves_source_do_not_submit_without_blocking_bridge() -> None:
    result = intersect_disclosure(
        TLPLevel.GREEN,
        True,
        True,
        DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=True,
            external_llm_allowed=True,
        ),
    )
    assert result.tlp is TLPLevel.GREEN
    assert result.do_not_submit is True
    assert result.external_llm_allowed is True


def test_handoff_keeps_external_llm_right_when_source_do_not_submit_is_true() -> None:
    policy = analyst_handoff_policy_from_sources(
        [
            SimpleNamespace(
                tlp="GREEN",
                do_not_submit=True,
                external_llm_allowed=True,
            )
        ]
    )

    assert policy.do_not_submit is True
    assert policy.external_llm_allowed is True


def test_capabilities_require_finite_dimensions_and_growth_only_extension() -> None:
    current = _capabilities()
    reduced_limits = dict(current.budget_limits)
    reduced_limits[BudgetDimension.VT_UNITS] -= 1
    with pytest.raises(ValueError, match="cannot reduce"):
        _capabilities(budget_limits=reduced_limits).require_growth_from(current)
    with pytest.raises(ValueError, match="finite"):
        _capabilities(budget_limits={BudgetDimension.VT_UNITS: 10})


class _MemoryPolicyRepository:
    def __init__(self) -> None:
        self.versions = []
        self.events = []

    async def lock_scope_index(self) -> None:
        return None

    async def lock_scope(self, *_args: object) -> None:
        return None

    async def latest_for_scope(self, kind, ref, *, for_update: bool = False):
        del for_update
        matching = [
            item for item in self.versions if item.scope_kind is kind and item.scope_ref == ref
        ]
        return max(matching, key=lambda item: item.version, default=None)

    async def add_version(self, policy, event) -> None:
        self.versions.append(policy)
        self.events.append(event)

    async def by_correlation(self, correlation_id: str):
        for event in self.events:
            if event.correlation_id == correlation_id:
                policy = next(
                    item
                    for item in self.versions
                    if item.policy_id == event.policy_id and item.version == event.policy_version
                )
                return policy, event
        return None


class _MemoryUow:
    def __init__(self) -> None:
        self.osint_open_policies = _MemoryPolicyRepository()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_activation_replay_is_idempotent_and_correlation_reuse_conflicts() -> None:
    uow = _MemoryUow()
    service = OsintOpenPolicyService(lambda: uow, _settings())  # type: ignore[arg-type]
    args = {
        "scope_kind": OsintScopeKind.INSTANCE,
        "scope_ref": "instance",
        "capabilities": _capabilities(),
        "actor_id": "human:operator-1",
        "reason": "authorized policy activation",
        "correlation_id": "activation-1",
    }
    activated = await service.activate(**args)
    replay = await service.activate(**args)
    assert replay.event_id == activated.event_id
    assert len(uow.osint_open_policies.versions) == 1
    assert len(uow.osint_open_policies.events) == 1

    with pytest.raises(OsintPolicyConflictError, match="reused"):
        await service.activate(**{**args, "reason": "different request"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("actor_id", "Bearer secret-value"),
        ("reason", "credential=secret-value"),
        ("correlation_id", "https://example.test/file?X-Amz-Signature=sentinel"),
    ),
)
async def test_audit_fields_reject_credential_and_signed_url_sentinels(
    field: str, value: str
) -> None:
    service = OsintOpenPolicyService(lambda: None, _settings())  # type: ignore[arg-type]
    args: dict[str, object] = {
        "scope_kind": OsintScopeKind.INSTANCE,
        "scope_ref": "instance",
        "capabilities": _capabilities(),
        "actor_id": "human:operator-1",
        "reason": "valid reason",
        "correlation_id": "safe-correlation",
    }
    args[field] = value
    with pytest.raises(OsintPolicyError, match="Unsafe"):
        await service.activate(**args)  # type: ignore[arg-type]


def test_activation_rejects_values_above_configured_hard_budgets() -> None:
    service = OsintOpenPolicyService(
        lambda: None,
        _settings(osint_open_max_vt_units=50),
    )  # type: ignore[arg-type]
    with pytest.raises(OsintPolicyError, match="exceeds"):
        service._validate_capabilities(_capabilities(vt_units=51))


def test_external_bridge_requires_envelope_and_intersects_source_flags() -> None:
    fixture = _Fixture(responses=[], tools=())
    assert not InvestigationService.external_llm_allowed(fixture.snapshot, None)
    event_id = uuid4()
    inherited = replace(
        fixture.envelope,
        approval_decision_id=event_id,
        authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
        inherited_policy_id=uuid4(),
        inherited_policy_version=1,
        inherited_policy_event_id=event_id,
    )
    assert InvestigationService.external_llm_allowed(fixture.snapshot, inherited)
    assert not InvestigationService.external_llm_allowed(
        replace(fixture.snapshot, external_llm_allowed=False), inherited
    )
    assert not InvestigationService.external_llm_allowed(
        replace(fixture.snapshot, tlp="AMBER"), inherited
    )
    for tlp in (None, "", "unknown", "RED"):
        assert not InvestigationService.external_llm_allowed(
            replace(fixture.snapshot, tlp=tlp), inherited
        )


@pytest.mark.asyncio
async def test_no_envelope_denies_bridge_submission_even_with_permissive_source() -> None:
    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    fixture.envelopes._items.pop(fixture.investigation_id)

    await fixture.service.run(state.investigation_id)

    assert fixture.snapshot.external_llm_allowed is True
    assert fixture.snapshot.do_not_submit is False
    assert fixture.bridge_transport.submission_count == 0


def _policy_authorized_envelope(envelope: InvestigationEnvelope) -> InvestigationEnvelope:
    event_id = uuid4()
    return replace(
        envelope,
        approval_decision_id=event_id,
        authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
        inherited_policy_id=uuid4(),
        inherited_policy_version=1,
        inherited_policy_event_id=event_id,
    )


@pytest.mark.asyncio
async def test_bridge_refuses_policy_pin_when_investigation_subject_changes() -> None:
    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    pinned_subject_id = fixture.subject_id
    fixture.envelopes._items[fixture.investigation_id] = _policy_authorized_envelope(
        fixture.envelope
    )
    investigation = fixture.investigations.items[fixture.investigation_id]
    fixture.investigations.items[fixture.investigation_id] = replace(
        investigation,
        subject_id=uuid4(),
    )

    async def effective_policy(_investigation_id):
        return SimpleNamespace(
            inheritance=SimpleNamespace(subject_id=pinned_subject_id),
            authorized=True,
        )

    fixture.service._effective_policy_for = effective_policy
    await fixture.service.run(state.investigation_id)

    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_bridge_refuses_revoked_policy_pin_before_submission() -> None:
    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    fixture.envelopes._items[fixture.investigation_id] = _policy_authorized_envelope(
        fixture.envelope
    )

    async def effective_policy(_investigation_id):
        return SimpleNamespace(
            inheritance=SimpleNamespace(subject_id=fixture.subject_id),
            authorized=False,
        )

    fixture.service._effective_policy_for = effective_policy
    await fixture.service.run(state.investigation_id)

    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_external_operator_tool_denied_without_envelope_but_local_read_remains_allowed() -> (
    None
):
    fixture = _Fixture(
        responses=[],
        tools=[
            ("vt.file_report", "vt"),
            ("local.read", "local", ToolEffect.LOCAL_READ, "local.read", {}),
        ],
    )
    state = await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    fixture.envelopes._items.pop(fixture.investigation_id)
    service = operator_service(fixture)

    with pytest.raises(OperatorToolInvocationError) as denied:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments={"query": "hash"},
            purpose="guard missing envelope",
            expected_version=state.version,
            idempotency_key="missing-envelope-external",
        )
    assert getattr(denied.value, "code", None) == "external_disclosure_denied"
    local = await service.invoke(
        tool_name="local.read",
        schema_version="1",
        arguments={"query": "local evidence"},
        purpose="local read remains available",
        expected_version=state.version,
        idempotency_key="missing-envelope-local",
    )
    assert local.invocation.status.value == "succeeded"


@pytest.mark.asyncio
async def test_external_operator_tool_rechecks_pinned_policy_status() -> None:
    fixture, state, _ = await operator_fixture()
    event_id = uuid4()
    fixture.envelopes._items[fixture.investigation_id] = replace(
        fixture.envelope,
        approval_decision_id=event_id,
        authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
        inherited_policy_id=uuid4(),
        inherited_policy_version=1,
        inherited_policy_event_id=event_id,
    )

    async def revoked_pin(_investigation_id):
        return SimpleNamespace(authorized=False)

    service = OperatorToolInvocationService(
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
        effective_policy_for=revoked_pin,
    )
    with pytest.raises(OperatorToolInvocationError) as error:
        await service.invoke(
            tool_name="vt.file_report",
            schema_version="1",
            arguments={"query": "hash"},
            purpose="recheck current policy status",
            expected_version=state.version,
            idempotency_key="revoked-policy-effect",
        )
    assert error.value.code == "external_disclosure_denied"
    assert fixture.tools["vt.file_report"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "qualification",
    (SampleQualification.CANDIDATE, SampleQualification.RELATED, SampleQualification.UNCERTAIN),
)
async def test_active_policy_does_not_weaken_g3_human_only_states(qualification) -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    policy_event = uuid4()
    database.envelopes[investigation.id] = InvestigationEnvelope(
        investigation_id=investigation.id,
        allowed_providers=("vt",),
        budget=InvestigationBudget(limits={BudgetDimension.VT_UNITS: 10}),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=policy_event,
        approval_actor_id="human:policy-activator",
        authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
        inherited_policy_id=uuid4(),
        inherited_policy_version=1,
        inherited_policy_event_id=policy_event,
    )
    association_id = uuid4()
    database.associations[association_id] = SampleAssociation(
        id=association_id,
        investigation_id=investigation.id,
        sample_sha256="b" * 64,
        origin=SampleOrigin.VT_PIVOT,
    )
    service = SampleQualificationService(lambda: FakeUow(database))

    updated = await service.qualify(
        association_id,
        qualification,
        InvestigationActor("agent:bridge", QualificationActorKind.AGENT),
        "Agent proposes a sample relation for human review.",
        (),
    )
    assert updated.qualification is qualification
    for human_only in (SampleQualification.CONFIRMED, SampleQualification.REJECTED):
        with pytest.raises(SampleQualificationPermissionError):
            await service.qualify(
                association_id,
                human_only,
                InvestigationActor("agent:bridge", QualificationActorKind.AGENT),
                "An agent cannot establish final membership.",
                (),
            )
    assert database.decisions == []


@pytest.mark.asyncio
async def test_operator_client_can_read_context_but_cannot_mutate_g2_or_policy() -> None:
    fixture, state, service = await operator_fixture()
    app = FastAPI()
    app.include_router(investigation_router)
    app.include_router(policy_router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = lambda: _Uow(fixture)
    app.state.investigation_composition_factory = _CompositionFactory(service, fixture=fixture)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        context = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator/context",
            params={"expected_version": state.version, "expected_sha256": state.sha256},
        )
        envelope_mutation = await client.post(
            f"/api/investigations/{fixture.investigation_id}/envelope/approve",
            json={"reason": "no", "correlation_id": "no", "expected_version": None},
            headers={"Idempotency-Key": "operator-client-g2"},
        )
        policy_mutation = await client.post(
            "/api/osint-open-policy/activate",
            json={
                "scope": {"kind": "instance", "subject_ids": []},
                "reason": "no",
                "correlation_id": "no",
            },
            headers={"Idempotency-Key": "operator-client-policy"},
        )

    assert context.status_code == 200, context.text
    assert envelope_mutation.status_code == 403
    assert policy_mutation.status_code == 403


@pytest.mark.asyncio
async def test_envelope_view_reconstructs_inherited_policy_event_lineage() -> None:
    fixture, _state, service = await operator_fixture()
    envelope = _policy_authorized_envelope(fixture.envelope)
    activation_correlation_id = "policy-activation-correlation"
    activation_event_id = uuid4()
    policy_event_id = uuid4()
    envelope = replace(
        envelope,
        approval_decision_id=policy_event_id,
        inherited_policy_version=2,
        inherited_policy_event_id=policy_event_id,
    )
    fixture.envelopes._items[fixture.investigation_id] = envelope
    policy_id = envelope.inherited_policy_id

    class _PolicyService:
        async def effective_for(self, uow, _investigation_id):
            assert uow.osint_open_policies is not None
            return SimpleNamespace(
                inheritance=SimpleNamespace(
                    policy_id=policy_id,
                    policy_version=2,
                    policy_event_id=policy_event_id,
                    activation_correlation_id=activation_correlation_id,
                    subject_id=fixture.subject_id,
                    status=SimpleNamespace(value="active"),
                ),
                policy=SimpleNamespace(actor_id="human:policy-extender"),
                revoked=False,
                authorized=True,
            )

    class _PolicyRepository:
        async def event_for_version(self, requested_policy_id, version):
            assert requested_policy_id == policy_id
            assert version == 1
            return SimpleNamespace(
                id=activation_event_id,
                policy_id=policy_id,
                policy_version=1,
                event_type="activated",
                actor_id="human:policy-activator",
                correlation_id=activation_correlation_id,
            )

    def uow_factory():
        uow = _Uow(fixture)
        uow.osint_open_policies = _PolicyRepository()
        return uow

    app = FastAPI()
    app.include_router(investigation_router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = uow_factory

    class _EnvelopeCompositionFactory(_CompositionFactory):
        def build(self, *args, **kwargs):
            composition = super().build(*args, **kwargs)
            composition.available_tools = ()
            return composition

    composition_factory = _EnvelopeCompositionFactory(service, fixture=fixture)
    composition_factory.osint_open_policy_service = _PolicyService()
    app.state.investigation_composition_factory = composition_factory

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/investigations/{fixture.investigation_id}/envelope")

    assert response.status_code == 200, response.text
    view = response.json()
    assert view["authority_kind"] == "osint_policy"
    assert view["inherited_policy_id"] == str(policy_id)
    assert view["inherited_policy_version"] == 2
    assert view["inherited_policy_event_id"] == str(policy_event_id)
    assert view["approval_decision_id"] == str(policy_event_id)
    assert view["approval_actor_id"] is None
    assert view["policy_activated_by"] == "human:policy-activator"
    assert view["inherited_subject_id"] == str(fixture.subject_id)


@pytest.mark.asyncio
async def test_operator_status_projects_policy_authority_without_individual_g2_approval() -> None:
    fixture, _state, service = await operator_fixture()
    envelope = _policy_authorized_envelope(fixture.envelope)
    activation_correlation_id = "status-policy-activation-correlation"
    activation_event_id = uuid4()
    policy_event_id = envelope.inherited_policy_event_id
    envelope = replace(
        envelope,
        inherited_policy_version=2,
        inherited_policy_event_id=policy_event_id,
    )
    fixture.envelopes._items[fixture.investigation_id] = envelope
    policy_id = envelope.inherited_policy_id

    class _PolicyService:
        async def effective_for(self, _uow, _investigation_id):
            return SimpleNamespace(
                inheritance=SimpleNamespace(
                    policy_id=policy_id,
                    policy_version=2,
                    policy_event_id=policy_event_id,
                    activation_correlation_id=activation_correlation_id,
                    subject_id=fixture.subject_id,
                    status=SimpleNamespace(value="active"),
                ),
                policy=SimpleNamespace(actor_id="human:policy-extender"),
                revoked=False,
                authorized=True,
            )

    class _PolicyRepository:
        async def event_for_version(self, requested_policy_id, version):
            assert requested_policy_id == policy_id
            assert version == 1
            return SimpleNamespace(
                id=activation_event_id,
                policy_id=policy_id,
                policy_version=1,
                event_type="activated",
                actor_id="human:policy-activator",
                correlation_id=activation_correlation_id,
            )

    def uow_factory():
        uow = _Uow(fixture)
        uow.osint_open_policies = _PolicyRepository()
        return uow

    app = FastAPI()
    app.include_router(investigation_router)
    app.state.identity_provider = _IdentityProvider()
    app.state.uow_factory = uow_factory

    class _StatusCompositionFactory(_CompositionFactory):
        def build(self, *args, **kwargs):
            composition = super().build(*args, **kwargs)
            composition.available_tools = ()
            return composition

    composition_factory = _StatusCompositionFactory(service, fixture=fixture)
    composition_factory.osint_open_policy_service = _PolicyService()
    app.state.investigation_composition_factory = composition_factory

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/investigations/{fixture.investigation_id}/operator-status"
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["g2"]["approval_decision_id"] is None
    assert payload["g2"]["approval_actor_id"] is None
    assert payload["policy_authority"] == {
        "policy_id": str(policy_id),
        "policy_version": 2,
        "policy_event_id": str(policy_event_id),
        "status": "active",
        "activated_by": "human:policy-activator",
    }


_MUTATION_REQUESTS = (
    (
        "/api/investigations/00000000-0000-0000-0000-000000000001/envelope/approve",
        {"reason": "review", "correlation_id": "approve", "expected_version": 1},
    ),
    (
        "/api/investigations/00000000-0000-0000-0000-000000000001/envelope/extend",
        {"reason": "review", "correlation_id": "extend", "expected_version": 1},
    ),
    (
        "/api/investigations/00000000-0000-0000-0000-000000000001/envelope/revoke",
        {"reason": "review", "correlation_id": "revoke", "expected_version": 1},
    ),
    (
        "/api/osint-open-policy/activate",
        {
            "scope": {"kind": "instance", "subject_ids": []},
            "reason": "review",
            "correlation_id": "activate",
        },
    ),
    (
        "/api/osint-open-policy/extend",
        {
            "policy_id": "00000000-0000-0000-0000-000000000002",
            "expected_version": 1,
            "reason": "review",
            "correlation_id": "policy-extend",
            "capabilities": {},
        },
    ),
    (
        "/api/osint-open-policy/revoke",
        {
            "policy_id": "00000000-0000-0000-0000-000000000002",
            "expected_version": 1,
            "reason": "review",
            "correlation_id": "policy-revoke",
        },
    ),
    (
        "/api/investigations/00000000-0000-0000-0000-000000000001/samples/00000000-0000-0000-0000-000000000003/qualify",
        {
            "expected_version": 1,
            "qualification": "confirmed",
            "reason": "review",
            "correlation_id": "confirm",
        },
    ),
    (
        "/api/investigations/00000000-0000-0000-0000-000000000001/samples/00000000-0000-0000-0000-000000000003/qualify",
        {
            "expected_version": 1,
            "qualification": "rejected",
            "reason": "review",
            "correlation_id": "reject",
        },
    ),
)


class _AuthenticatedPrincipal:
    mode = "authenticated"
    verified = True

    def __init__(self, actor_id: str) -> None:
        self.actor_id = actor_id

    async def current(self) -> Identity:
        return Identity(
            actor_id=self.actor_id,
            scopes=frozenset({IdentityScope.HUMAN_OPERATOR}),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_id", ("agent:bridge", "system:automation"))
@pytest.mark.parametrize(("path", "payload"), _MUTATION_REQUESTS)
async def test_agent_and_system_principals_cannot_use_any_human_mutation_route(
    actor_id: str, path: str, payload: dict[str, object]
) -> None:
    app = FastAPI()
    app.include_router(investigation_router)
    app.include_router(policy_router)
    app.state.identity_provider = _AuthenticatedPrincipal(actor_id)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            json=payload,
            headers={"Idempotency-Key": f"reject-{actor_id}"},
        )

    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "verified_human_operator_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "payload"), _MUTATION_REQUESTS)
async def test_human_mutation_routes_reject_client_supplied_actor_override(
    path: str, payload: dict[str, object]
) -> None:
    app = FastAPI()
    app.include_router(investigation_router)
    app.include_router(policy_router)
    app.state.identity_provider = _AuthenticatedPrincipal("human:verified-operator")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            json={**payload, "actor_id": "human:spoofed-client"},
            headers={"Idempotency-Key": "reject-client-actor-override"},
        )

    assert response.status_code == 422, response.text


def test_api_capabilities_accept_shodan_with_a_positive_credit_limit() -> None:
    from cti_app.api.osint_policy import PolicyCapabilitiesRequest, _capabilities

    service = OsintOpenPolicyService(lambda: None, _settings())  # type: ignore[arg-type]
    capabilities = _capabilities(
        service,
        PolicyCapabilitiesRequest(
            allowed_providers=["bridge", "vt", "shodan"],
            budget_limits={"shodan_credits": 1000},
        ),
    )

    assert set(capabilities.allowed_providers) == {"bridge", "vt", "shodan"}
    assert capabilities.budget_limits[BudgetDimension.SHODAN_CREDITS] == 1000

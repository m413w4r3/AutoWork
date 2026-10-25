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
from cti_app.application.investigation_governance import (
    InvestigationActor,
    SampleQualificationPermissionError,
    SampleQualificationService,
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
    DisclosureBounds,
    EnvelopeAuthorityKind,
    InvestigationBudget,
    InvestigationEnvelope,
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


def test_disclosure_intersection_never_clears_source_restrictions() -> None:
    result = intersect_disclosure(
        TLPLevel.GREEN,
        True,
        True,
        DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
    )
    assert result.tlp is TLPLevel.GREEN
    assert result.do_not_submit is True
    assert result.external_llm_allowed is False


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


@pytest.mark.asyncio
async def test_external_operator_tool_denied_without_envelope_but_local_read_remains_allowed() -> (
    None
):
    fixture = _Fixture(
        responses=[],
        tools=[("vt.file_report", "vt"), ("local.read", "local", ToolEffect.LOCAL_READ)],
    )
    state = await fixture.start()
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

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from cti_app.application.investigation_governance import (
    EnvelopeRevokedError,
    EnvelopeVersionConflictError,
    InvestigationActor,
    InvestigationEnvelopeService,
    SampleQualificationPermissionError,
    SampleQualificationService,
)
from cti_app.config import Settings
from cti_app.domain.editorial import AnalystDecisionType
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    InvestigationBudget,
    QualificationActorKind,
    SampleQualification,
    TLPLevel,
)
from tests.investigation_c2_fakes import (
    FakeDatabase,
    FakeUow,
    make_approved_proposal,
    make_investigation,
)


@pytest.mark.asyncio
async def test_default_proposal_is_local_only_and_requires_a_separate_g2_approval() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    service = InvestigationEnvelopeService(
        lambda: FakeUow(database), settings=Settings(_env_file=None)
    )

    proposal = service.propose_default(investigation.id)

    assert proposal.approved is False
    assert proposal.allowed_providers == ()
    assert proposal.acquire_hits_allowed is False
    assert proposal.budget.limits[BudgetDimension.VT_UNITS] == 0
    assert proposal.budget.limits[BudgetDimension.DOWNLOADS] == 0
    assert proposal.budget.limits[BudgetDimension.TOOL_SECONDS] == 3600
    assert proposal.budget.limits[BudgetDimension.CONTEXT_BYTES] == 4_000_000
    assert proposal.budget.limits[BudgetDimension.CONTEXT_TOKENS] == 1_000_000
    assert proposal.disclosure.do_not_submit is True
    assert proposal.disclosure.external_llm_allowed is False
    assert await service.current(investigation.id) is None
    assert database.decisions == []


@pytest.mark.asyncio
async def test_g2_approval_persists_actor_reason_and_is_idempotent() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    service = InvestigationEnvelopeService(lambda: FakeUow(database))
    proposal = make_approved_proposal(investigation)

    envelope = await service.approve(
        proposal,
        actor_id="analyst:alice",
        reason="VT pivots may acquire up to ten candidate hits.",
        correlation_id="g2:alice:01",
    )
    replay = await service.approve(
        proposal,
        actor_id="analyst:alice",
        reason="VT pivots may acquire up to ten candidate hits.",
        correlation_id="g2:alice:01",
    )

    assert envelope == replay
    assert envelope.version == 1
    assert envelope.approval_actor_id == "analyst:alice"
    assert len(database.decisions) == 1
    decision = database.decisions[0]
    assert decision.decision_type is AnalystDecisionType.PIVOT_APPROVE
    assert decision.actor_id == "analyst:alice"
    assert decision.reason == "VT pivots may acquire up to ten candidate hits."
    assert decision.correlation_id == "g2:alice:01"


@pytest.mark.asyncio
async def test_budget_extension_is_a_distinct_cas_g2_decision() -> None:
    database = FakeDatabase()
    investigation = make_investigation(max_hits=1, max_vt_units=1)
    investigation.budget.max_new_samples = 1
    database.investigations[investigation.id] = investigation
    service = InvestigationEnvelopeService(lambda: FakeUow(database))
    initial = await service.approve(
        make_approved_proposal(investigation, vt_units=1, downloads=1),
        actor_id="analyst:alice",
        reason="One candidate download for this review.",
        correlation_id="g2:initial",
    )
    initial.budget.reserve(
        "first-acquisition",
        {BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 1},
    )
    initial.budget.commit("first-acquisition")

    extended = await service.extend(
        investigation.id,
        expected_version=initial.version,
        allowed_providers=("vt",),
        budget=InvestigationBudget(
            limits={BudgetDimension.VT_UNITS: 3, BudgetDimension.DOWNLOADS: 3}
        ),
        acquire_hits_allowed=True,
        disclosure=initial.disclosure,
        actor_id="analyst:alice",
        reason="The first hit exposed another relevant branch.",
        correlation_id="g2:extension-1",
    )

    assert extended.version == 2
    assert extended.budget.limits[BudgetDimension.DOWNLOADS] == 3
    assert extended.budget.consumed[BudgetDimension.DOWNLOADS] == 1
    assert investigation.budget.max_hits_acquired == 3
    assert investigation.budget.max_new_samples == 3
    assert investigation.budget.max_vt_read_units == 3
    assert len(database.decisions) == 2
    assert database.decisions[-1].decision_type is AnalystDecisionType.PIVOT_APPROVE
    with pytest.raises(EnvelopeVersionConflictError, match="revocation state"):
        await service.reject(
            investigation.id,
            actor_id="analyst:alice",
            reason="An active envelope needs an explicit revocation model.",
            correlation_id="g2:reject-active",
            expected_version=extended.version,
        )
    with pytest.raises(EnvelopeVersionConflictError):
        await service.extend(
            investigation.id,
            expected_version=1,
            allowed_providers=("vt",),
            budget=InvestigationBudget(
                limits={BudgetDimension.VT_UNITS: 9, BudgetDimension.DOWNLOADS: 6}
            ),
            acquire_hits_allowed=True,
            disclosure=initial.disclosure,
            actor_id="analyst:alice",
            reason="Stale version must not overwrite G2.",
            correlation_id="g2:extension-stale",
        )

    revoked = await service.revoke(
        investigation.id,
        expected_version=extended.version,
        actor_id="analyst:alice",
        reason="The external review window is closed.",
        correlation_id="g2:revoke",
    )
    assert revoked.budget.limits[BudgetDimension.DOWNLOADS] == 0
    assert investigation.budget.max_hits_acquired == 3
    assert investigation.budget.max_new_samples == 3
    assert investigation.budget.max_vt_read_units == 3


@pytest.mark.asyncio
async def test_g2_rejection_is_a_persisted_pre_approval_decision() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    service = InvestigationEnvelopeService(lambda: FakeUow(database))

    decision = await service.reject(
        investigation.id,
        actor_id="analyst:alice",
        reason="The proposed provider scope is too broad.",
        correlation_id="g2:reject:1",
    )
    replay = await service.reject(
        investigation.id,
        actor_id="analyst:alice",
        reason="The proposed provider scope is too broad.",
        correlation_id="g2:reject:1",
    )

    assert decision == replay
    assert decision.decision_type is AnalystDecisionType.PIVOT_REJECT
    assert decision.target_id == investigation.id
    assert await service.current(investigation.id) is None
    assert len(database.decisions) == 1


@pytest.mark.asyncio
async def test_revoke_then_explicit_reextension_preserves_scope_and_consumption() -> None:
    database = FakeDatabase()
    investigation = make_investigation(max_hits=4, max_vt_units=4)
    database.investigations[investigation.id] = investigation
    service = InvestigationEnvelopeService(lambda: FakeUow(database))
    proposal = make_approved_proposal(
        investigation,
        allowed_providers=("vt",),
        acquire_hits_allowed=False,
        vt_units=4,
        downloads=0,
    )
    proposal = replace(
        proposal,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
    )
    initial = await service.approve(
        proposal,
        actor_id="analyst:alice",
        reason="Approve bounded VT and Bridge work.",
        correlation_id="g2:revoke-extend:initial",
    )
    initial.budget.reserve("already-completed-vt", {BudgetDimension.VT_UNITS: 1})
    initial.budget.commit("already-completed-vt")
    revoked = await service.revoke(
        investigation.id,
        expected_version=initial.version,
        actor_id="analyst:alice",
        reason="Stop external work.",
        correlation_id="g2:revoke-extend:revoke",
    )
    authorization = await service.authorization_state(investigation.id)
    assert authorization.revoked is True
    assert authorization.authorization_ceiling is not None
    assert revoked.budget.consumed[BudgetDimension.VT_UNITS] == 1

    extended = await service.extend(
        investigation.id,
        expected_version=revoked.version,
        allowed_providers=("vt",),
        budget=InvestigationBudget(
            limits={BudgetDimension.VT_UNITS: 4, BudgetDimension.DOWNLOADS: 0}
        ),
        acquire_hits_allowed=False,
        disclosure=initial.disclosure,
        actor_id="analyst:alice",
        reason="Explicitly resume within the prior authorization.",
        correlation_id="g2:revoke-extend:extend",
    )

    assert extended.allowed_providers == ("vt",)
    assert extended.budget.limits[BudgetDimension.VT_UNITS] == 4
    assert extended.budget.consumed[BudgetDimension.VT_UNITS] == 1
    assert extended.budget.remaining[BudgetDimension.VT_UNITS] == 3
    assert extended.version == revoked.version + 1
    final_state = await service.authorization_state(investigation.id)
    assert final_state.revoked is False


@pytest.mark.parametrize(
    ("providers", "vt_units", "disclosure", "message"),
    (
        (
            ("vt", "shodan"),
            4,
            DisclosureBounds(TLPLevel.AMBER, True, False),
            "prior G2 provider authorization",
        ),
        (
            ("vt",),
            5,
            DisclosureBounds(TLPLevel.AMBER, True, False),
            "prior G2 vt_units budget",
        ),
        (
            ("vt",),
            4,
            DisclosureBounds(TLPLevel.GREEN, True, False),
            "widen the approved TLP",
        ),
        (
            ("vt",),
            4,
            DisclosureBounds(TLPLevel.AMBER, False, False),
            "cannot remove do_not_submit",
        ),
    ),
)
@pytest.mark.asyncio
async def test_revoked_envelope_reextension_cannot_exceed_approved_bounds(
    providers, vt_units, disclosure, message
) -> None:
    database = FakeDatabase()
    investigation = make_investigation(max_hits=4, max_vt_units=4)
    database.investigations[investigation.id] = investigation
    service = InvestigationEnvelopeService(lambda: FakeUow(database))
    proposal = replace(
        make_approved_proposal(
            investigation,
            allowed_providers=("vt",),
            acquire_hits_allowed=False,
            vt_units=4,
            downloads=0,
        ),
        disclosure=DisclosureBounds(
            tlp=TLPLevel.AMBER,
            do_not_submit=True,
            external_llm_allowed=False,
        ),
    )
    initial = await service.approve(
        proposal,
        actor_id="analyst:alice",
        reason="Approve one constrained VT scope.",
        correlation_id="g2:bounds:initial",
    )
    revoked = await service.revoke(
        investigation.id,
        expected_version=initial.version,
        actor_id="analyst:alice",
        reason="Stop the approved scope.",
        correlation_id="g2:bounds:revoke",
    )

    with pytest.raises(ValueError, match=message):
        await service.extend(
            investigation.id,
            expected_version=revoked.version,
            allowed_providers=providers,
            budget=InvestigationBudget(
                limits={BudgetDimension.VT_UNITS: vt_units, BudgetDimension.DOWNLOADS: 0}
            ),
            acquire_hits_allowed=False,
            disclosure=disclosure,
            actor_id="analyst:alice",
            reason="Request an extension outside the prior G2 bounds.",
            correlation_id=f"g2:bounds:extend:{message}",
        )

    current = await service.current(investigation.id)
    assert current == revoked
    assert len(database.decisions) == 2


@pytest.mark.asyncio
async def test_revoked_reextension_cannot_reduce_budget_below_consumed_usage() -> None:
    database = FakeDatabase()
    investigation = make_investigation(max_hits=4, max_vt_units=4)
    database.investigations[investigation.id] = investigation
    service = InvestigationEnvelopeService(lambda: FakeUow(database))
    initial = await service.approve(
        make_approved_proposal(
            investigation,
            allowed_providers=("vt",),
            acquire_hits_allowed=False,
            vt_units=4,
            downloads=0,
        ),
        actor_id="analyst:alice",
        reason="Approve one bounded VT scope.",
        correlation_id="g2:consumed-budget:initial",
    )
    initial.budget.reserve("already-consumed", {BudgetDimension.VT_UNITS: 1})
    initial.budget.commit("already-consumed")
    revoked = await service.revoke(
        investigation.id,
        expected_version=initial.version,
        actor_id="analyst:alice",
        reason="Stop the approved scope.",
        correlation_id="g2:consumed-budget:revoke",
    )

    with pytest.raises(ValueError, match="below its consumed or reserved usage"):
        await service.extend(
            investigation.id,
            expected_version=revoked.version,
            allowed_providers=("vt",),
            budget=InvestigationBudget(
                limits={BudgetDimension.VT_UNITS: 0, BudgetDimension.DOWNLOADS: 0}
            ),
            acquire_hits_allowed=False,
            disclosure=initial.disclosure,
            actor_id="analyst:alice",
            reason="The previous VT read remains chargeable.",
            correlation_id="g2:consumed-budget:extend-too-small",
        )

    assert await service.current(investigation.id) == revoked
    assert len(database.decisions) == 2


@pytest.mark.asyncio
async def test_legacy_revocation_without_saved_bounds_requires_new_investigation() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    service = InvestigationEnvelopeService(lambda: FakeUow(database))
    initial = await service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Approve a bounded investigation.",
        correlation_id="g2:legacy-revoke:initial",
    )
    revoked = await service.revoke(
        investigation.id,
        expected_version=initial.version,
        actor_id="analyst:alice",
        reason="Revoke a pre-upgrade envelope.",
        correlation_id="g2:legacy-revoke:revoke",
    )
    database.decisions[-1] = replace(database.decisions[-1], details={})

    with pytest.raises(EnvelopeRevokedError, match="create a new investigation"):
        await service.extend(
            investigation.id,
            expected_version=revoked.version,
            allowed_providers=("vt",),
            budget=InvestigationBudget(
                limits={BudgetDimension.VT_UNITS: 20, BudgetDimension.DOWNLOADS: 10}
            ),
            acquire_hits_allowed=True,
            disclosure=initial.disclosure,
            actor_id="analyst:alice",
            reason="Try to reopen without durable pre-revoke bounds.",
            correlation_id="g2:legacy-revoke:extend",
        )

    assert await service.current(investigation.id) == revoked


@pytest.mark.asyncio
async def test_confirmed_membership_requires_human_g3_decision() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    association_id = uuid4()
    from cti_app.domain.investigation import SampleAssociation, SampleOrigin

    database.associations[association_id] = SampleAssociation(
        id=association_id,
        investigation_id=investigation.id,
        sample_sha256="b" * 64,
        origin=SampleOrigin.VT_PIVOT,
    )
    service = SampleQualificationService(lambda: FakeUow(database))

    with pytest.raises(SampleQualificationPermissionError):
        await service.qualify(
            association_id,
            SampleQualification.CONFIRMED,
            InvestigationActor("agent:bridge", QualificationActorKind.AGENT),
            "Static features look similar.",
            (),
        )

    updated = await service.qualify(
        association_id,
        SampleQualification.CONFIRMED,
        InvestigationActor("analyst:alice", QualificationActorKind.HUMAN),
        "The analyst reviewed the linked evidence and validated membership.",
        (),
        correlation_id="g3:member:1",
    )

    assert updated.qualification is SampleQualification.CONFIRMED
    assert updated.version == 2
    assert len(updated.changes) == 1
    assert updated.changes[0].actor_id == "analyst:alice"
    assert len(database.decisions) == 1
    assert database.decisions[0].decision_type is AnalystDecisionType.MEMBER_VALIDATE
    assert database.decisions[0].target_id == association_id

"""M24-03B oracles: one bounded G2 envelope for the VT bootstrap.

The harness reuses the real M24-03A bootstrap service (inventory/plan/batch),
the real VT tool adapters, the real acquisition quarantine path and the real
``InvestigationEnvelopeService`` (approve/revoke/AuthorDecision audit).  Only
the VT HTTP port is a local fake; no test contacts a provider.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.investigation_bootstrap_envelope import (
    router as bootstrap_envelope_router,
)
from cti_app.application.identity import Identity, IdentityScope, LocalIdentityProvider
from cti_app.application.investigation_bootstrap import (
    BootstrapPlanState,
    BootstrapReason,
    BootstrapRouteStatus,
)
from cti_app.application.investigation_bootstrap_envelope import (
    PROPOSAL_SCHEMA_VERSION,
    BootstrapBudgetLimitSource,
    BootstrapEnvelopeApprovalV1,
    BootstrapEnvelopeProposalStaleError,
    BootstrapEnvelopeProposalV1,
    InvestigationBootstrapEnvelopeService,
)
from cti_app.application.investigation_governance import InvestigationEnvelopeService
from cti_app.application.virustotal import VirusTotalQuotaExceededError
from cti_app.config import Settings
from cti_app.domain.editorial import AnalystDecisionType
from cti_app.domain.investigation import BudgetDimension, ToolInvocationStatus
from tests.investigation_c2_fakes import _Decisions
from tests.test_investigation_bootstrap import (
    _DEFAULT_SEED,
    _DOMAIN,
    _IP,
    _URL,
    _BootstrapUow,
    _Harness,
    _harness,
    _IocSpec,
    _NoopExecutor,
)

# The fake ArticleSnapshot always carries TLP GREEN and external_llm_allowed.
_HUMAN = "analyst:alice"
_REASON = "Approve the single bounded bootstrap envelope proposed by the workshop."


class _EnvelopeUow(_BootstrapUow):
    """03A in-memory UoW plus the append-only AnalystDecision repository."""

    async def __aenter__(self) -> _EnvelopeUow:
        await super().__aenter__()
        self.analyst_decisions = _Decisions(self.db)
        return self


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "investigation_max_vt_read_units": 200,
        "investigation_max_hits_acquired": 10,
        "virustotal_download_max_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _uow_factory(harness: _Harness) -> Callable[[], _EnvelopeUow]:
    return lambda: _EnvelopeUow(harness.db)


def _service(
    harness: _Harness, settings: Settings | None = None
) -> InvestigationBootstrapEnvelopeService:
    return InvestigationBootstrapEnvelopeService(
        uow_factory=_uow_factory(harness),
        bootstrap=harness.bootstrap,
        article_snapshots=harness.article_port,
        settings=settings or _settings(),
        clock=lambda: harness.now,
    )


def _limits(proposal: BootstrapEnvelopeProposalV1) -> dict[BudgetDimension, int]:
    return {item.dimension: item.limit for item in proposal.costs.limits}


def _set_article_policy(
    harness: _Harness,
    *,
    tlp: str | None = "GREEN",
    do_not_submit: bool = False,
    external_llm_allowed: bool = True,
) -> None:
    harness.snapshot = replace(
        harness.snapshot,
        tlp=tlp,
        do_not_submit=do_not_submit,
        external_llm_allowed=external_llm_allowed,
    )
    harness.article_port.snapshot = harness.snapshot


class _CountingExecutor:
    def __init__(self, executor: Any) -> None:
        self.executor = executor
        self.calls = 0

    async def execute_tool_call(self, **kwargs: Any) -> Any:
        self.calls += 1
        return await self.executor.execute_tool_call(**kwargs)


class _RevokingExecutor:
    """Revoke through the real envelope service after the first dispatched call."""

    def __init__(
        self,
        harness: _Harness,
        executor: Any,
        envelope_service: InvestigationEnvelopeService,
    ) -> None:
        self.harness = harness
        self.executor = executor
        self.envelope_service = envelope_service
        self.calls = 0

    async def execute_tool_call(self, **kwargs: Any) -> Any:
        self.calls += 1
        result = await self.executor.execute_tool_call(**kwargs)
        if self.calls == 1:
            current = self.harness.db.envelopes[self.harness.investigation.id]
            await self.envelope_service.revoke(
                self.harness.investigation.id,
                expected_version=current.version,
                actor_id=_HUMAN,
                reason="Revoke the bootstrap authority during the batch.",
                correlation_id="g2:bootstrap:revoke-mid-batch",
            )
        return result


async def _propose_and_approve(
    harness: _Harness,
    *,
    settings: Settings | None = None,
    correlation_id: str = "g2:bootstrap:oracle",
) -> tuple[BootstrapEnvelopeProposalV1, BootstrapEnvelopeApprovalV1]:
    service = _service(harness, settings)
    proposal = await service.propose_for_bootstrap(harness.investigation.id)
    approval = await service.approve_for_bootstrap(
        harness.investigation.id,
        actor_id=_HUMAN,
        reason=_REASON,
        correlation_id=correlation_id,
        expected_proposal_sha256=proposal.proposal_sha256,
    )
    return proposal, approval


# ---------------------------------------------------------------------------
# ADR oracle
# ---------------------------------------------------------------------------


async def _assert_single_bounded_envelope_requires_human_approval() -> None:
    harness = _harness(with_envelope=False)
    service = _service(harness)

    proposal = await service.propose_for_bootstrap(harness.investigation.id)

    assert proposal.approved is False
    assert proposal.already_approved is False
    assert proposal.allowed_providers == ("vt", "bridge")
    assert proposal.acquire_hits_allowed is True
    assert harness.db.envelopes == {}
    assert harness.db.decisions == []
    assert sum(harness.vt.calls.values()) == 0
    assert harness.vt.download_calls == []

    approval = await service.approve_for_bootstrap(
        harness.investigation.id,
        actor_id=_HUMAN,
        reason=_REASON,
        correlation_id="g2:bootstrap:human-approval",
        expected_proposal_sha256=proposal.proposal_sha256,
    )

    assert approval.envelope_version == 1
    assert approval.approval_actor_id == _HUMAN
    assert approval.allowed_providers == ("vt", "bridge")
    assert approval.budget_limits[BudgetDimension.VT_UNITS.value] == sum(
        item.limit for item in proposal.costs.limits if item.dimension is BudgetDimension.VT_UNITS
    )
    assert len(harness.db.decisions) == 1
    decision = harness.db.decisions[0]
    assert decision.decision_type is AnalystDecisionType.PIVOT_APPROVE
    assert decision.target_id == harness.investigation.id
    assert decision.actor_id == _HUMAN
    assert sum(harness.vt.calls.values()) == 0
    assert harness.vt.download_calls == []

    replay = await service.approve_for_bootstrap(
        harness.investigation.id,
        actor_id=_HUMAN,
        reason=_REASON,
        correlation_id="g2:bootstrap:human-approval",
        expected_proposal_sha256=proposal.proposal_sha256,
    )
    assert replay.envelope_version == approval.envelope_version
    assert len(harness.db.decisions) == 1


@pytest.mark.asyncio
async def test_single_bounded_envelope_requires_human_approval() -> None:
    await _assert_single_bounded_envelope_requires_human_approval()


@pytest.mark.asyncio
async def test_b04_single_envelope_requires_human_approval() -> None:
    await _assert_single_bounded_envelope_requires_human_approval()


@pytest.mark.asyncio
async def test_stale_proposal_is_refused_before_approval() -> None:
    harness = _harness(with_envelope=False)
    service = _service(harness)

    with pytest.raises(BootstrapEnvelopeProposalStaleError) as refused:
        await service.approve_for_bootstrap(
            harness.investigation.id,
            actor_id=_HUMAN,
            reason=_REASON,
            correlation_id="g2:bootstrap:stale",
            expected_proposal_sha256="0" * 64,
        )
    assert refused.value.code == "bootstrap_proposal_stale"
    assert harness.db.envelopes == {}
    assert harness.db.decisions == []


@pytest.mark.asyncio
async def test_g2_absent_makes_zero_provider_calls() -> None:
    harness = _harness(with_envelope=False)
    service = _service(harness)

    proposal = await service.propose_for_bootstrap(harness.investigation.id)
    assert proposal.allowed_providers
    assert proposal.plan_state is BootstrapPlanState.AWAITING_G2

    snapshot = await harness.run(executor=_NoopExecutor(), idempotency_key="no-g2-run")
    assert snapshot.plan_state is BootstrapPlanState.AWAITING_G2
    assert snapshot.limits.stopped_reason is BootstrapReason.AWAITING_G2
    assert sum(harness.vt.calls.values()) == 0
    assert harness.vt.download_calls == []
    assert harness.db.envelopes == {}


# ---------------------------------------------------------------------------
# B05/B09 scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b05_approved_envelope_covers_planned_ops_without_per_hash() -> None:
    harness = _harness(with_envelope=False)
    proposal, approval = await _propose_and_approve(harness)

    assert proposal.vt_reads.operations_covered == proposal.vt_reads.operations_planned
    assert proposal.sample_acquisition.sample_count_covered == 1
    assert proposal.sample_acquisition.acquire_hits_allowed is True
    assert sum(harness.vt.calls.values()) == 0

    counting = _CountingExecutor(harness.executor)
    snapshot = await harness.run(executor=counting, idempotency_key="approved-batch")

    assert snapshot.plan_state is BootstrapPlanState.READY
    assert counting.calls == proposal.covered_operations
    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.download_calls == [_DEFAULT_SEED]
    assert all(
        operation.status is not ToolInvocationStatus.POLICY_DENIED
        for ioc in snapshot.iocs
        for operation in ioc.operations
    )
    assert approval.envelope_version == snapshot.envelope_version


@pytest.mark.asyncio
async def test_operations_outside_the_approved_envelope_refuse_typed() -> None:
    harness = _harness(with_envelope=False)
    settings = _settings(investigation_max_vt_read_units=2, investigation_max_hits_acquired=0)
    proposal, _ = await _propose_and_approve(
        harness,
        settings=settings,
        correlation_id="g2:bootstrap:partial-envelope",
    )

    assert proposal.vt_reads.operations_covered == 1
    assert proposal.vt_reads.operations_planned > proposal.vt_reads.operations_covered
    assert proposal.sample_acquisition.acquire_hits_allowed is False

    snapshot = await harness.run(idempotency_key="partial-envelope-batch")

    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.calls["behaviours"] == 0
    assert harness.vt.calls["object_relationship"] == 0
    assert harness.vt.download_calls == []
    operations = [operation for ioc in snapshot.iocs for operation in ioc.operations]
    assert any(operation.reason is BootstrapReason.BUDGET_EXCEEDED for operation in operations)
    download = next(
        operation for operation in operations if operation.tool_name == "vt.download_sample"
    )
    assert download.status is ToolInvocationStatus.POLICY_DENIED
    assert download.reason is BootstrapReason.ACQUISITION_NOT_ALLOWED
    assert all(
        operation.status
        in {
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.NEEDS_REVIEW,
            ToolInvocationStatus.POLICY_DENIED,
        }
        for operation in operations
    )


@pytest.mark.asyncio
async def test_quota_exhaustion_mid_batch_stops_further_calls() -> None:
    harness = _harness(with_envelope=False)
    await _propose_and_approve(harness, correlation_id="g2:bootstrap:quota")
    harness.vt.errors["file_report"] = VirusTotalQuotaExceededError("quota reached")

    snapshot = await harness.run(idempotency_key="quota-approved-batch")

    assert snapshot.limits.stopped_reason is BootstrapReason.QUOTA
    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.calls["behaviours"] == 0
    assert harness.vt.calls["object_relationship"] == 0
    assert harness.vt.download_calls == []


@pytest.mark.asyncio
async def test_b09_download_is_covered_once_by_the_approved_envelope() -> None:
    harness = _harness(with_envelope=False)
    proposal, _ = await _propose_and_approve(harness, correlation_id="g2:bootstrap:download")

    assert proposal.sample_acquisition.allowed is True
    assert proposal.sample_acquisition.sample_count_covered == 1
    assert proposal.sample_acquisition.total_max_bytes == 1024 * 1024
    assert proposal.sample_acquisition.downloads_limit == 1

    snapshot = await harness.run(idempotency_key="download-batch")
    assert snapshot.plan_state is BootstrapPlanState.READY
    assert harness.vt.download_calls == [_DEFAULT_SEED]
    association = next(
        item
        for item in harness.db.associations.values()
        if item.investigation_id == harness.investigation.id and item.sample_sha256 == _DEFAULT_SEED
    )
    assert association.qualification.value == "candidate"
    assert snapshot.iocs[0].download is not None
    assert snapshot.iocs[0].download.hash_verified_quarantine is True


@pytest.mark.asyncio
async def test_b09_revocation_mid_batch_stops_without_resumption() -> None:
    harness = _harness(with_envelope=False)
    await _propose_and_approve(harness, correlation_id="g2:bootstrap:revocation")
    envelope_service = InvestigationEnvelopeService(_uow_factory(harness), settings=_settings())
    revoking = _RevokingExecutor(harness, harness.executor, envelope_service)

    snapshot = await harness.run(executor=revoking, idempotency_key="revoked-batch")

    # One successful dispatch plus one admission attempt refused by the revoked
    # envelope; no provider call is emitted after the revocation.
    assert revoking.calls == 2
    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.calls["behaviours"] == 0
    assert harness.vt.calls["object_relationship"] == 0
    assert harness.vt.download_calls == []
    failed = [
        (operation.tool_name, operation.status.value, operation.error_code)
        for ioc in snapshot.iocs
        for operation in ioc.operations
    ]
    assert snapshot.limits.stopped_reason is BootstrapReason.POLICY_DENIED, failed
    assert any(
        operation.reason is BootstrapReason.STOPPED
        for ioc in snapshot.iocs
        for operation in ioc.operations
    )
    authorization = await envelope_service.authorization_state(harness.investigation.id)
    assert authorization.revoked is True

    resumed = await harness.run(
        executor=_NoopExecutor(), idempotency_key="revoked-batch-after-revocation"
    )
    assert resumed.plan_state is not BootstrapPlanState.READY
    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.download_calls == []


# ---------------------------------------------------------------------------
# Bounds and separated dimensions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_caps_and_download_route_are_visible_in_the_proposal() -> None:
    harness = _harness(with_envelope=False)
    settings = _settings(
        investigation_max_vt_read_units=7,
        investigation_max_hits_acquired=2,
        virustotal_download_max_bytes=4321,
    )
    proposal = await _service(harness, settings).propose_for_bootstrap(harness.investigation.id)

    # The configuration ceilings are carried in the human-visible proposal.
    assert proposal.costs.caps.vt_units == 7
    assert proposal.costs.caps.downloads == 2
    assert proposal.costs.caps.per_sample_bytes == 4321
    limits = {item.dimension: item for item in proposal.costs.limits}
    assert limits[BudgetDimension.VT_UNITS].limit == 7
    # One cited sample bounds the effective download budget; the settings cap
    # stays visible separately in ``costs.caps`` and is never exceeded.
    assert limits[BudgetDimension.DOWNLOADS].limit == 1
    assert limits[BudgetDimension.DOWNLOADS].source is BootstrapBudgetLimitSource.INVENTORY

    # The download route stays explicitly unverified: the proposal promises no
    # successful acquisition and documents the runtime route failure vocabulary.
    assert proposal.sample_acquisition.route_status is BootstrapRouteStatus.UNVERIFIED
    assert any("route_unavailable" in item for item in proposal.sample_acquisition.limitations)


@pytest.mark.asyncio
async def test_proposal_never_exceeds_settings_caps() -> None:
    harness = _harness(with_envelope=False)
    settings = _settings(investigation_max_vt_read_units=10, investigation_max_hits_acquired=1)
    proposal = await _service(harness, settings).propose_for_bootstrap(harness.investigation.id)

    limits = _limits(proposal)
    assert limits[BudgetDimension.VT_UNITS] <= settings.investigation_max_vt_read_units
    assert limits[BudgetDimension.DOWNLOADS] <= settings.investigation_max_hits_acquired
    assert proposal.vt_reads.units_proposed <= settings.investigation_max_vt_read_units
    assert proposal.sample_acquisition.sample_count_covered <= (
        settings.investigation_max_hits_acquired
    )
    assert proposal.sample_acquisition.total_max_bytes <= (
        proposal.sample_acquisition.sample_count_covered * settings.virustotal_download_max_bytes
    )
    assert proposal.vt_reads.operations_covered < proposal.vt_reads.operations_planned
    assert proposal.sample_acquisition.acquire_hits_allowed is False


@pytest.mark.asyncio
async def test_external_llm_allowed_false_is_never_widened() -> None:
    harness = _harness(with_envelope=False)
    _set_article_policy(harness, external_llm_allowed=False)
    proposal = await _service(harness).propose_for_bootstrap(harness.investigation.id)

    assert proposal.external_model.allowed is False
    assert proposal.external_model.providers == ()
    assert proposal.disclosure.external_llm_allowed is False
    assert proposal.allowed_providers == ()
    assert proposal.vt_reads.allowed is False
    assert proposal.vt_reads.operations_covered == 0
    assert proposal.sample_acquisition.acquire_hits_allowed is False
    limits = _limits(proposal)
    assert limits[BudgetDimension.VT_UNITS] == 0
    assert limits[BudgetDimension.DOWNLOADS] == 0
    assert any("external_llm_allowed" in item for item in proposal.limitations)


@pytest.mark.asyncio
async def test_each_authority_dimension_is_reported_separately() -> None:
    harness = _harness(
        with_envelope=False,
        specs=(
            _IocSpec("sha256", _DEFAULT_SEED, context="row 1"),
            _IocSpec("ipv4", _IP, context="row 2"),
            _IocSpec("domain", _DOMAIN, context="row 3"),
            _IocSpec("url", _URL, context="row 4"),
        ),
    )
    proposal = await _service(harness).propose_for_bootstrap(harness.investigation.id)

    assert proposal.inventory.samples == 1
    assert proposal.inventory.ipv4 == 1
    assert proposal.inventory.domains == 1
    assert proposal.inventory.urls == 1
    assert proposal.vt_reads.summary
    assert proposal.sample_acquisition.summary
    assert proposal.costs.summary
    assert proposal.external_model.summary
    assert proposal.disclosure.summary
    assert proposal.vt_reads.per_tool
    assert {item.tool_name for item in proposal.vt_reads.per_tool} == {
        "vt.file_report",
        "vt.object_report",
        "vt.relationships",
        "vt.behaviour",
    }
    limits = _limits(proposal)
    assert set(limits) == {
        BudgetDimension.VT_UNITS,
        BudgetDimension.DOWNLOADS,
        BudgetDimension.TOOL_SECONDS,
        BudgetDimension.CONTEXT_BYTES,
        BudgetDimension.CONTEXT_TOKENS,
    }
    assert proposal.disclosure.tlp.value == "green"
    assert proposal.disclosure.scope.value == "article_cited_iocs"
    assert proposal.external_model.allowed is True
    assert proposal.vt_reads.allowed is True
    assert proposal.sample_acquisition.allowed is True
    assert proposal.planned_operations == proposal.covered_operations
    assert proposal.covered_ioc_ids
    assert PROPOSAL_SCHEMA_VERSION == "vt-bootstrap-envelope-proposal.v1"


@pytest.mark.asyncio
async def test_do_not_submit_does_not_veto_vt_or_bridge_authority() -> None:
    harness = _harness(with_envelope=False)
    _set_article_policy(harness, do_not_submit=True, external_llm_allowed=True)
    proposal = await _service(harness).propose_for_bootstrap(harness.investigation.id)

    assert proposal.disclosure.do_not_submit is True
    assert proposal.vt_reads.allowed is True
    assert "vt" in proposal.allowed_providers
    assert "bridge" in proposal.allowed_providers
    assert proposal.covered_operations > 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _route_app(harness: _Harness, settings: Settings) -> FastAPI:
    app = FastAPI()
    app.include_router(bootstrap_envelope_router)
    app.state.uow_factory = _uow_factory(harness)
    app.state.settings = settings
    app.state.investigation_composition_factory = SimpleNamespace(
        article_snapshots=harness.article_port,
        artifact_store=harness.artifact_store,
        build=lambda _uow, _investigation_id, operator_mode=False: SimpleNamespace(
            service=harness.executor
        ),
    )
    return app


@pytest.mark.asyncio
async def test_bootstrap_envelope_routes_require_verified_human() -> None:
    harness = _harness(with_envelope=False)
    settings = _settings(
        virustotal_proxy_url="http://vt-proxy.test",
        virustotal_file_report_enabled=True,
        virustotal_object_relationships_enabled=True,
        virustotal_behaviour_reports_enabled=True,
        virustotal_file_download_enabled=True,
    )
    app = _route_app(harness, settings)

    class _OperatorClient:
        async def current(self) -> Identity:
            return Identity(
                actor_id="operator:operator-client",
                scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
            )

    app.state.identity_provider = _OperatorClient()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused_get = await client.get(
            f"/api/investigations/{harness.investigation.id}/bootstrap/envelope-proposal"
        )
        assert refused_get.status_code == 403
        refused_post = await client.post(
            f"/api/investigations/{harness.investigation.id}/bootstrap/envelope/approve",
            headers={"Idempotency-Key": "api-envelope-approval"},
            json={
                "reason": _REASON,
                "correlation_id": "g2:api:bootstrap",
                "proposal_sha256": "0" * 64,
            },
        )
        assert refused_post.status_code == 403

    app.state.identity_provider = LocalIdentityProvider("dev-analyst")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        proposal_response = await client.get(
            f"/api/investigations/{harness.investigation.id}/bootstrap/envelope-proposal"
        )
        assert proposal_response.status_code == 200, proposal_response.text
        payload = proposal_response.json()
        assert payload["approved"] is False
        assert payload["proposal_sha256"]
        assert payload["vt_reads"]["allowed"] is True
        assert payload["sample_acquisition"]["allowed"] is True
        assert payload["external_model"]["allowed"] is True

        approval = await client.post(
            f"/api/investigations/{harness.investigation.id}/bootstrap/envelope/approve",
            headers={"Idempotency-Key": "api-envelope-approval"},
            json={
                "reason": _REASON,
                "correlation_id": "g2:api:bootstrap",
                "proposal_sha256": payload["proposal_sha256"],
            },
        )
        assert approval.status_code == 200, approval.text
        approved = approval.json()
        assert approved["approved"] is True
        assert approved["envelope_version"] == 1
        assert approved["approval_actor_id"] == "dev-analyst"
        assert len(harness.db.decisions) == 1

        replay = await client.post(
            f"/api/investigations/{harness.investigation.id}/bootstrap/envelope/approve",
            headers={"Idempotency-Key": "api-envelope-approval"},
            json={
                "reason": _REASON,
                "correlation_id": "g2:api:bootstrap",
                "proposal_sha256": payload["proposal_sha256"],
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["envelope_version"] == 1
        assert len(harness.db.decisions) == 1

        stale = await client.post(
            f"/api/investigations/{harness.investigation.id}/bootstrap/envelope/approve",
            headers={"Idempotency-Key": "api-envelope-approval-stale"},
            json={
                "reason": _REASON,
                "correlation_id": "g2:api:bootstrap-stale",
                "proposal_sha256": "0" * 64,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "bootstrap_proposal_stale"

        unknown = await client.get(f"/api/investigations/{uuid4()}/bootstrap/envelope-proposal")
        assert unknown.status_code == 404

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from cti_app.api.investigation import router as investigation_router
from cti_app.application.investigation_governance import InvestigationEnvelopeService
from cti_app.application.osint_open_policy import OsintOpenPolicyService
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EnvelopeAuthorityKind,
    InvestigationBudget,
    InvestigationEnvelope,
    LoopBudget,
    TLPLevel,
    ToolInvocation,
)
from cti_app.domain.osint_open import OsintCapabilities, OsintInheritanceStatus, OsintScopeKind
from cti_app.domain.production import (
    AnalystInvestigation,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


def _capabilities(vt_units: int = 100) -> OsintCapabilities:
    return OsintCapabilities(
        allowed_providers=("bridge", "shodan", "vt"),
        budget_limits={
            BudgetDimension.VT_UNITS: vt_units,
            BudgetDimension.SHODAN_CREDITS: 20,
            BudgetDimension.DOWNLOADS: 10,
            BudgetDimension.BRIDGE_TURNS: 20,
            BudgetDimension.TOOL_SECONDS: 3600,
            BudgetDimension.CONTEXT_BYTES: 1_000_000,
            BudgetDimension.CONTEXT_TOKENS: 100_000,
        },
        acquire_hits_allowed=True,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        max_effect_claims_per_investigation=25,
    )


def _settings() -> Settings:
    return Settings(_env_file=None)


def _service(session_factory) -> OsintOpenPolicyService:
    return OsintOpenPolicyService(
        _uow_factory(session_factory),
        _settings(),
    )


def _uow_factory(session_factory):
    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    return factory


async def _make_investigation(session_factory, *, subject_id=None) -> AnalystInvestigation:
    from tests.integration.edition_codes import reserve_edition_code

    suffix = uuid4().hex
    if subject_id is None:
        edition = Edition(
            country=f"OSINT policy {suffix}",
            country_code=reserve_edition_code(),
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            tlp=TLP.AMBER,
            languages=("en",),
        )
        subject = Subject(
            edition_id=edition.id,
            title="OSINT policy integration fixture",
            slug=f"osint-policy-{suffix}",
            tlp=TLP.AMBER,
        )
    else:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            subject = await uow.subjects.get(subject_id)
        assert subject is not None
        edition = None
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="f" * 64,
    )
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=artifact.id,
        budget=LoopBudget(),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        if edition is not None:
            assert await uow.editions.add_if_absent(edition)
            await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


async def _activate_for_subject(service: OsintOpenPolicyService, subject_id):
    return await service.activate(
        scope_kind=OsintScopeKind.SUBJECT_SET,
        scope_ref=str(subject_id),
        capabilities=_capabilities(),
        actor_id="human:integration-operator",
        reason="authorized integration fixture",
        correlation_id=f"activate:{subject_id}",
    )


def _snapshot(investigation):
    return SimpleNamespace(
        subject_id=investigation.subject_id,
        tlp="GREEN",
        do_not_submit=False,
        external_llm_allowed=True,
    )


async def _start_with_policy(service, factory, investigation):
    async with factory() as uow:
        durable = await uow.analyst_investigations.get(investigation.id)
        envelope = await service.ensure_inherited_envelope(
            uow,
            investigation=durable,
            snapshot=_snapshot(durable),
            at_start=True,
        )
        await uow.commit()
        return envelope


@pytest.mark.asyncio
async def test_activation_persists_one_version_and_append_only_event_on_replay(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    service = _service(session_factory)
    investigation = await _make_investigation(session_factory)
    try:
        activated = await _activate_for_subject(service, investigation.subject_id)
        replay = await _activate_for_subject(service, investigation.subject_id)
        assert replay.event_id == activated.event_id

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            current = await uow.osint_open_policies.latest_for_scope(
                OsintScopeKind.SUBJECT_SET, str(investigation.subject_id)
            )
            event = await uow.osint_open_policies.event_for_version(
                activated.policy_id, activated.version
            )
            assert current == activated
            assert event is not None
            assert event.id == activated.event_id
            assert len(await uow.osint_open_policies.list_events(activated.policy_id)) == 1
            await uow.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_start_pins_one_policy_envelope_without_human_decision(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    factory = _uow_factory(session_factory)
    service = _service(session_factory)
    investigation = await _make_investigation(session_factory)
    try:
        policy = await _activate_for_subject(service, investigation.subject_id)
        left, right = await asyncio.gather(
            _start_with_policy(service, factory, investigation),
            _start_with_policy(service, factory, investigation),
        )
        assert left is not None and right is not None
        assert left.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY
        assert right.approval_decision_id == policy.event_id == left.approval_decision_id
        assert left.version == right.version == 1
        async with factory() as uow:
            pin = await uow.osint_open_policies.get_inheritance(investigation.id)
            decisions = await uow.analyst_decisions.list_for_investigation(investigation.id)
            assert pin is not None
            assert pin.subject_id == investigation.subject_id
            assert pin.policy_version == 1
            assert pin.correlation_id == f"osint-inherit:{policy.policy_id}:1:{investigation.id}"
            assert decisions == ()
            await uow.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_policy_extension_does_not_upgrade_existing_pin_or_budget(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    factory = _uow_factory(session_factory)
    service = _service(session_factory)
    investigation = await _make_investigation(session_factory)
    try:
        initial = await _activate_for_subject(service, investigation.subject_id)
        envelope = await _start_with_policy(service, factory, investigation)
        assert envelope is not None
        extended = await service.extend(
            policy_id=initial.policy_id,
            expected_version=initial.version,
            capabilities=_capabilities(vt_units=200),
            actor_id="human:integration-operator",
            reason="extend active ceiling",
            correlation_id=f"extend:{investigation.subject_id}",
        )
        async with factory() as uow:
            effective = await service.effective_for(uow, investigation.id)
            current_envelope = await uow.investigation_envelopes.get(investigation.id)
            assert extended.version == 2
            assert effective is not None and effective.policy.version == 1
            assert current_envelope is not None
            assert current_envelope.budget.limits[BudgetDimension.VT_UNITS] == 100
            assert effective.authorized
            await uow.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_prior_invocation_unpinned_and_manual_g2_cases_do_not_auto_inherit(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    factory = _uow_factory(session_factory)
    service = _service(session_factory)
    old = await _make_investigation(session_factory)
    manual = await _make_investigation(session_factory, subject_id=old.subject_id)
    latest_new_inv = await _make_investigation(session_factory, subject_id=old.subject_id)
    try:
        policy = await _activate_for_subject(service, old.subject_id)
        async with factory() as uow:
            await uow.tool_invocations.add_if_absent(
                ToolInvocation(
                    investigation_id=old.id,
                    operation_key=f"old-call:{old.id}",
                    tool_name="vt.file_report",
                    schema_version="1",
                    normalized_parameters={"query": "synthetic"},
                    purpose="pre-policy invocation fixture",
                )
            )
            old_durable = await uow.analyst_investigations.get(old.id)
            assert (
                await service.ensure_inherited_envelope(
                    uow,
                    investigation=old_durable,
                    snapshot=_snapshot(old_durable),
                    at_start=True,
                )
                is None
            )

            manual_durable = await uow.analyst_investigations.get(manual.id)
            manual_envelope = InvestigationEnvelope(
                investigation_id=manual.id,
                allowed_providers=("vt",),
                budget=InvestigationBudget(limits=_capabilities().budget_limits),
                acquire_hits_allowed=False,
                disclosure=DisclosureBounds(
                    tlp=TLPLevel.GREEN,
                    do_not_submit=False,
                    external_llm_allowed=True,
                ),
                approval_decision_id=uuid4(),
                approval_actor_id="human:manual-g2",
                authority_kind=EnvelopeAuthorityKind.HUMAN_G2,
            )
            await uow.investigation_envelopes.save_if_version(
                manual_envelope, expected_version=None
            )
            result = await service.ensure_inherited_envelope(
                uow,
                investigation=manual_durable,
                snapshot=_snapshot(manual_durable),
                at_start=True,
            )
            assert result == manual_envelope
            assert await uow.osint_open_policies.get_inheritance(manual.id) is None
            await uow.commit()

        async with factory() as uow:
            latest_durable = await uow.analyst_investigations.get(latest_new_inv.id)
            no_auto_inherit = await service.ensure_inherited_envelope(
                uow,
                investigation=latest_durable,
                snapshot=_snapshot(latest_durable),
                at_start=False,
            )
            assert no_auto_inherit is None
            assert policy.status.value == "active"
            await uow.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_revocation_updates_api_view_effective_status_and_preserves_local_budgets(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    factory = _uow_factory(session_factory)
    service = _service(session_factory)
    settings = _settings()
    investigation = await _make_investigation(session_factory)
    try:
        policy = await _activate_for_subject(service, investigation.subject_id)
        envelope = await _start_with_policy(service, factory, investigation)
        assert envelope is not None
        budget = envelope.budget
        budget.reserve(
            "reserved-effect",
            {
                BudgetDimension.VT_UNITS: 3,
                BudgetDimension.TOOL_SECONDS: 30,
                BudgetDimension.CONTEXT_BYTES: 128,
            },
        )
        reserved = replace(envelope, budget=budget, version=envelope.version + 1)
        async with factory() as uow:
            await uow.investigation_envelopes.save_if_version(
                reserved, expected_version=envelope.version
            )
            await uow.commit()

        revoked = await service.revoke(
            policy_id=policy.policy_id,
            expected_version=policy.version,
            actor_id="human:integration-operator",
            reason="end policy authorization",
            correlation_id=f"revoke:{investigation.subject_id}",
        )
        async with factory() as uow:
            effective = await service.effective_for(uow, investigation.id)
            current = await uow.investigation_envelopes.get(investigation.id)
            pin = await uow.osint_open_policies.get_inheritance(investigation.id)
            assert revoked.status.value == "revoked"
            assert effective is not None and effective.revoked and not effective.authorized
            assert current is not None and current.disclosure.external_llm_allowed is False
            assert current.disclosure.tlp is envelope.disclosure.tlp
            assert current.disclosure.do_not_submit is envelope.disclosure.do_not_submit
            assert current.budget.limits[BudgetDimension.VT_UNITS] == 0
            assert current.budget.limits[BudgetDimension.SHODAN_CREDITS] == 0
            assert current.budget.limits[BudgetDimension.DOWNLOADS] == 0
            for dimension in (
                BudgetDimension.TOOL_SECONDS,
                BudgetDimension.CONTEXT_BYTES,
                BudgetDimension.CONTEXT_TOKENS,
            ):
                assert current.budget.limits[dimension] == envelope.budget.limits[dimension]
            assert current.budget.reserved[BudgetDimension.TOOL_SECONDS] == 30
            assert current.budget.reserved[BudgetDimension.CONTEXT_BYTES] == 128
            assert current.budget.reserved[BudgetDimension.VT_UNITS] == 3
            assert pin is not None and pin.status is OsintInheritanceStatus.REVOKED
            await uow.commit()

        class _SnapshotResolver:
            async def resolve(self, _artifact_id):
                return _snapshot(investigation)

        composition = SimpleNamespace(
            tool_dependencies=SimpleNamespace(article_snapshots=_SnapshotResolver()),
            available_tools=(),
        )

        class _CompositionFactory:
            def __init__(self) -> None:
                self.settings = settings
                self.osint_open_policy_service = service

            def build(self, *_args, **_kwargs):
                return composition

        app = FastAPI()
        app.include_router(investigation_router)
        app.state.uow_factory = factory
        app.state.investigation_composition_factory = _CompositionFactory()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/investigations/{investigation.id}/envelope")
        assert response.status_code == 200, response.text
        assert response.json()["policy_status"] == "revoked"
        assert response.json()["currently_authorized"] is False

        async with factory() as uow:
            current = await uow.investigation_envelopes.get(investigation.id)
            assert current is not None
        manual = await InvestigationEnvelopeService(factory, settings=settings).extend(
            investigation.id,
            expected_version=current.version,
            allowed_providers=current.allowed_providers,
            budget=current.budget,
            acquire_hits_allowed=current.acquire_hits_allowed,
            disclosure=current.disclosure,
            actor_id="human:manual-g2",
            reason="re-authorize only within retained bounds",
            correlation_id=f"manual-extend:{investigation.subject_id}",
        )
        assert manual.authority_kind is EnvelopeAuthorityKind.HUMAN_G2
        assert manual.inherited_policy_id is None
        async with factory() as uow:
            pin = await uow.osint_open_policies.get_inheritance(investigation.id)
            assert pin is not None and pin.status is OsintInheritanceStatus.SUPERSEDED
            await uow.commit()

        no_new_policy = await _make_investigation(
            session_factory, subject_id=investigation.subject_id
        )
        assert await _start_with_policy(service, factory, no_new_policy) is None
        reactivated = await service.activate(
            scope_kind=OsintScopeKind.SUBJECT_SET,
            scope_ref=str(investigation.subject_id),
            capabilities=_capabilities(),
            actor_id="human:integration-operator",
            reason="new policy authority after revocation",
            correlation_id=f"reactivate:{investigation.subject_id}",
        )
        new_policy_case = await _make_investigation(
            session_factory, subject_id=investigation.subject_id
        )
        new_envelope = await _start_with_policy(service, factory, new_policy_case)
        assert reactivated.version == 3
        assert new_envelope is not None
        assert new_envelope.inherited_policy_version == reactivated.version
    finally:
        await engine.dispose()


def test_migration_upgrades_a_database_from_0002_to_osint_open_head(
    temporary_postgres_url: str,
) -> None:
    config = Config(Path(__file__).resolve().parents[2] / "alembic.ini")
    config.set_main_option("sqlalchemy.url", temporary_postgres_url.replace("%", "%%"))
    config.config_file_name = None
    command.upgrade(config, "0002_c2_experiments")

    async def _restore_legacy_0002_envelope_shape() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE investigation_envelopes "
                        "DROP CONSTRAINT IF EXISTS ck_investigation_envelopes_authority_lineage"
                    )
                )
                await connection.execute(
                    text(
                        "ALTER TABLE investigation_envelopes "
                        "DROP COLUMN IF EXISTS inherited_policy_event_id, "
                        "DROP COLUMN IF EXISTS inherited_policy_version, "
                        "DROP COLUMN IF EXISTS inherited_policy_id, "
                        "DROP COLUMN IF EXISTS authority_kind"
                    )
                )
        finally:
            await engine.dispose()

    async def _has_policy_table() -> bool:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.connect() as connection:
                return bool(
                    await connection.scalar(
                        text("SELECT to_regclass('osint_open_policies') IS NOT NULL")
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_restore_legacy_0002_envelope_shape())
    assert asyncio.run(_has_policy_table()) is False
    command.upgrade(config, "head")
    assert asyncio.run(_has_policy_table()) is True

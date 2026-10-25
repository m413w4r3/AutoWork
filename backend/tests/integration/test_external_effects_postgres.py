from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.external_effects import (
    Ambiguous,
    Completed,
    ConsumeCommitted,
    ConsumePolicyContext,
    ExternalEffectCoordinator,
    PermitHandoffGate,
    Refused,
    TransportResponse,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.external_effects import (
    ConsumedEffectPermit,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
    FrozenWireRequest,
    PreparedExternalRequest,
    canonical_json_bytes,
    effect_key,
    occurrence_sha256,
)
from cti_app.domain.investigation import (
    AgendaItem,
    InvestigationAgenda,
    InvestigationExecutionMode,
)
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.external_effects import (
    ExternalEffectClaimRow,
    ExternalEffectUseRow,
)
from cti_app.infrastructure.database.models.investigation import (
    InvestigationBudgetRecordRow,
    InvestigationBudgetReservationAmountRow,
    InvestigationBudgetReservationRow,
    InvestigationEnvelopeRow,
    ToolInvocationRow,
)
from cti_app.infrastructure.database.models.production import AnalystInvestigationRow
from cti_app.infrastructure.database.repositories.external_effects import (
    ExternalEffectFamilyBinding,
    SqlAlchemyExternalEffectClaimStore,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.workflow_emulator.investigation_support import seed_verified_article

pytestmark = pytest.mark.integration

_FAMILY = "vt.file_report"
_PROVIDER = "virustotal"
_NORMALIZER = "vt.file-report.v1"
_ARGUMENTS = {"hash": "abc"}
_REQUEST = {"hash": "abc", "include": ["relations", "history"]}


class _CountingTransport:
    def __init__(self) -> None:
        self.calls = 0
        self.send_entries = 0
        self._lock = asyncio.Lock()

    async def send(
        self,
        request: FrozenWireRequest,
        *,
        permit: ConsumedEffectPermit,
        prepared: PreparedExternalRequest,
        gate: PermitHandoffGate,
    ) -> TransportResponse:
        self.send_entries += 1
        await gate.enter(permit, prepared)
        async with self._lock:
            self.calls += 1
        return TransportResponse(result_ref="artifact:pg-fake-result")


class _BarrierPolicyGuard:
    """Barrier guard with policy-event lock before envelope lock in both paths."""

    def __init__(self, policy_event_id: UUID, *, block_consume: bool = False) -> None:
        self.policy_event_id = policy_event_id
        self.lock_key = policy_event_id.int & ((1 << 63) - 1)
        self.policy_version = "osint-open.v1"
        self.revoked = False
        self.block_consume = block_consume
        self.consume_locked = asyncio.Event()
        self.release_consume = asyncio.Event()
        self.revoke_started = asyncio.Event()
        self.events: list[str] = []

    async def lock_for_consume(self, database_session: AsyncSession, authority: Any) -> bool:
        self.events.append("policy_lock")
        if authority.policy_event_id != self.policy_event_id:
            return False
        if authority.policy_version != self.policy_version:
            return False
        await database_session.execute(
            text("SELECT pg_advisory_xact_lock(:policy_lock_key)"),
            {"policy_lock_key": self.lock_key},
        )
        self.consume_locked.set()
        if self.block_consume:
            await self.release_consume.wait()
        return not self.revoked

    async def authorize(self, context: ConsumePolicyContext) -> bool:
        self.events.append("policy_authorize")
        return (
            bool(context.database_session.in_transaction())
            and not self.revoked
            and context.investigation_subject_id == context.authority.pinned_subject_id
        )

    async def revoke(self, session_factory, investigation_id: UUID) -> None:
        self.revoke_started.set()
        async with session_factory() as session:
            async with session.begin():
                # Policy-event then envelope is the shared consume/revoke order.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:policy_lock_key)"),
                    {"policy_lock_key": self.lock_key},
                )
                await session.scalar(
                    select(InvestigationEnvelopeRow)
                    .where(InvestigationEnvelopeRow.investigation_id == investigation_id)
                    .with_for_update()
                )
                self.revoked = True


class _ClaimBarrierStore:
    """Hold both workers immediately before the durable claim transaction."""

    def __init__(self, delegate: SqlAlchemyExternalEffectClaimStore, barrier: asyncio.Barrier):
        self._delegate = delegate
        self._barrier = barrier

    async def consume(self, *args: Any, **kwargs: Any):
        await self._barrier.wait()
        return await self._delegate.consume(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _PreConsumeFailureStore:
    """Inject a crash before the real store opens a transaction."""

    def __init__(self, delegate: SqlAlchemyExternalEffectClaimStore) -> None:
        self._delegate = delegate
        self.consume_calls = 0

    async def consume(self, *args: Any, **kwargs: Any):
        self.consume_calls += 1
        raise RuntimeError("injected crash before consume")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


async def _prepare_case(
    database_url: str,
    tmp_path: Path,
    *,
    suffix: str,
    invocations: int = 1,
) -> tuple[
    async_sessionmaker[AsyncSession],
    AsyncEngine,
    UUID,
    UUID,
    list[tuple[ExternalEffectAuthority, PreparedExternalRequest]],
]:
    engine = create_postgres_engine(database_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    artifact_store = ProductionArtifactStore(
        BlobCatalogService(
            FilesystemBlobStore(tmp_path / f"external-effects-{suffix}"), uow_factory
        )
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=suffix,
        include_seed=False,
    )
    subject_id = seeded.subject.id
    approval_id = uuid4()
    now = datetime.now(UTC)
    authorities: list[tuple[ExternalEffectAuthority, PreparedExternalRequest]] = []
    async with session_factory() as session:
        async with session.begin():
            await session.execute(update_investigation_status(seeded.investigation.id))
            await session.execute(
                insert(InvestigationBudgetRecordRow)
                .values(investigation_id=seeded.investigation.id, version=1, updated_at=now)
                .on_conflict_do_nothing(
                    index_elements=[InvestigationBudgetRecordRow.investigation_id]
                )
            )
            await session.execute(
                insert(InvestigationEnvelopeRow)
                .values(
                    investigation_id=seeded.investigation.id,
                    allowed_providers=[_PROVIDER],
                    budget_snapshot={},
                    acquire_hits_allowed=True,
                    disclosure_tlp="clear",
                    disclosure_do_not_submit=False,
                    disclosure_external_llm_allowed=True,
                    approval_decision_id=approval_id,
                    approval_actor_id="integration-test",
                    approved_at=now,
                    updated_at=now,
                    version=4,
                )
                .on_conflict_do_update(
                    index_elements=[InvestigationEnvelopeRow.investigation_id],
                    set_={
                        "allowed_providers": [_PROVIDER],
                        "budget_snapshot": {},
                        "acquire_hits_allowed": True,
                        "disclosure_tlp": "clear",
                        "disclosure_do_not_submit": False,
                        "disclosure_external_llm_allowed": True,
                        "approval_decision_id": approval_id,
                        "approval_actor_id": "integration-test",
                        "approved_at": now,
                        "updated_at": now,
                        "version": 4,
                    },
                )
            )
            for index in range(invocations):
                operation_key = f"external-effect-op-{uuid4()}"
                invocation_id = uuid4()
                session.add(
                    ToolInvocationRow(
                        id=invocation_id,
                        investigation_id=seeded.investigation.id,
                        operation_key=operation_key,
                        tool_name=_FAMILY,
                        schema_version="v1",
                        normalized_parameters=_ARGUMENTS,
                        purpose="S3 external-effect integration fixture",
                        status="running",
                        reserved_budget={"vt_units": 1},
                        consumed_budget={},
                        response_refs=[],
                        created_at=now,
                        updated_at=now,
                        version=1,
                    )
                )
                session.add(
                    InvestigationBudgetReservationRow(
                        investigation_id=seeded.investigation.id,
                        operation_key=operation_key,
                        status="reserved",
                        sequence=1000 + index,
                    )
                )
                await session.flush()
                session.add(
                    InvestigationBudgetReservationAmountRow(
                        investigation_id=seeded.investigation.id,
                        operation_key=operation_key,
                        dimension="vt_units",
                        reserved_amount=1,
                        committed_amount=None,
                    )
                )
                authority = ExternalEffectAuthority(
                    investigation_id=seeded.investigation.id,
                    invocation_id=invocation_id,
                    operation_key=operation_key,
                    reservation_amounts={"vt_units": 1},
                    expected_envelope_version=4,
                    approval_decision_id=approval_id,
                    invocation_arguments_sha256=hashlib.sha256(
                        canonical_json_bytes(_ARGUMENTS)
                    ).hexdigest(),
                )
                prepared = PreparedExternalRequest.build(
                    family=_FAMILY,
                    provider=_PROVIDER,
                    provider_scope="tenant-test",
                    normalizer_version=_NORMALIZER,
                    canonical_request=_REQUEST,
                    application_request=FrozenWireRequest(
                        method=b"GET",
                        target=b"/v1/report?hash=abc",
                        headers=((b"accept", b"application/json"),),
                        body=b"",
                    ),
                )
                authorities.append((authority, prepared))
    async with uow_factory() as uow:
        await uow.investigation_states.save_if_version(
            InvestigationWorkingState(
                investigation_id=seeded.investigation.id,
                agenda=InvestigationAgenda(
                    items=(
                        AgendaItem(question="Verify the source claim.", potential=0.9, rank=1),
                        AgendaItem(question="Check for contradictions.", potential=0.7, rank=2),
                    )
                ),
                execution_mode=InvestigationExecutionMode.BRIDGE_LED,
            ),
            expected_version=None,
        )
        await uow.commit()
    return session_factory, engine, seeded.investigation.id, subject_id, authorities


def update_investigation_status(investigation_id: UUID):
    from sqlalchemy import update

    return (
        update(AnalystInvestigationRow)
        .where(AnalystInvestigationRow.id == investigation_id)
        .values(status="running")
    )


def _store(
    session_factory: async_sessionmaker[AsyncSession], *, policy_guard: Any = None
) -> SqlAlchemyExternalEffectClaimStore:
    return SqlAlchemyExternalEffectClaimStore(
        session_factory,
        policy_guard=policy_guard,
        family_bindings={
            _FAMILY: ExternalEffectFamilyBinding(
                provider=_PROVIDER,
                provider_scopes=frozenset({"tenant-test"}),
                normalizer_versions=frozenset({_NORMALIZER}),
                invocation_tool_binding=(_FAMILY, "v1"),
            )
        },
    )


@pytest.mark.asyncio
async def test_concurrent_same_effect_and_replay_make_one_send(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url,
        tmp_path,
        suffix=uuid4().hex[:12],
        invocations=2,
    )
    store = _store(session_factory)
    coordinator = ExternalEffectCoordinator(
        _ClaimBarrierStore(store, asyncio.Barrier(len(authorities)))
    )
    transport = _CountingTransport()
    try:
        results = await asyncio.gather(
            *(
                coordinator.send_one(authority, prepared, transport)
                for authority, prepared in authorities
            )
        )
        assert transport.calls == 1
        assert sum(isinstance(result, Completed) for result in results) == 1
        assert sum(isinstance(result, (Refused, Ambiguous)) for result in results) == 1
        first_authority, first_prepared = authorities[0]
        replay = await coordinator.send_one(first_authority, first_prepared, transport)
        assert isinstance(replay, Refused)
        assert replay.reason == "effect_occurrence_required"
        assert transport.calls == 1
        async with session_factory() as session:
            claim_count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(
                    ExternalEffectClaimRow.investigation_id == investigation_id,
                    ExternalEffectClaimRow.effect_key
                    == effect_key(investigation_id, first_prepared),
                )
            )
            sent_count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectUseRow)
                .where(
                    ExternalEffectUseRow.claim_id
                    == next(result.claim_id for result in results if isinstance(result, Completed)),
                    ExternalEffectUseRow.use_kind == "SENT",
                )
            )
        assert claim_count == 1
        assert sent_count == 1
    finally:
        await engine.dispose()


def _policy_authority(
    base: ExternalEffectAuthority, *, subject_id: UUID, policy_event_id: UUID
) -> ExternalEffectAuthority:
    return ExternalEffectAuthority(
        investigation_id=base.investigation_id,
        invocation_id=base.invocation_id,
        operation_key=base.operation_key,
        reservation_amounts=base.reservation_amounts,
        expected_envelope_version=base.expected_envelope_version,
        approval_decision_id=base.approval_decision_id,
        invocation_arguments_sha256=base.invocation_arguments_sha256,
        authority_kind=ExternalEffectAuthorityKind.OSINT_POLICY,
        policy_event_id=policy_event_id,
        policy_version="osint-open.v1",
        pinned_subject_id=subject_id,
    )


@pytest.mark.asyncio
async def test_revocation_first_guard_denies_with_zero_send(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    base, prepared = authorities[0]
    guard = _BarrierPolicyGuard(uuid4())
    authority = _policy_authority(
        base, subject_id=subject_id, policy_event_id=guard.policy_event_id
    )
    store = _store(session_factory, policy_guard=guard)
    transport = _CountingTransport()
    try:
        await guard.revoke(session_factory, investigation_id)
        outcome = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "osint_policy_denied"
        assert guard.events == ["policy_lock"]
        assert transport.calls == 0
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.investigation_id == investigation_id)
            )
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_consume_first_policy_lock_wins_before_revocation(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    base, prepared = authorities[0]
    guard = _BarrierPolicyGuard(uuid4(), block_consume=True)
    authority = _policy_authority(
        base, subject_id=subject_id, policy_event_id=guard.policy_event_id
    )
    store = _store(session_factory, policy_guard=guard)
    transport = _CountingTransport()
    try:
        send_task = asyncio.create_task(
            ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        )
        await asyncio.wait_for(guard.consume_locked.wait(), timeout=10)
        revoke_task = asyncio.create_task(guard.revoke(session_factory, investigation_id))
        await asyncio.wait_for(guard.revoke_started.wait(), timeout=10)
        guard.release_consume.set()
        outcome = await send_task
        await revoke_task
        assert isinstance(outcome, Completed)
        assert transport.calls == 1
        assert guard.events == ["policy_lock", "policy_authorize"]
        async with session_factory() as session:
            claim = await session.get(ExternalEffectClaimRow, outcome.claim_id)
        assert claim is not None and claim.state == "COMPLETED"
    finally:
        guard.release_consume.set()
        await engine.dispose()


@pytest.mark.asyncio
async def test_crash_before_consume_leaves_no_claim(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    failing_store = _PreConsumeFailureStore(store)
    transport = _CountingTransport()
    coordinator = ExternalEffectCoordinator(failing_store)
    try:
        outcome = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "consume_unavailable"
        assert failing_store.consume_calls == 1
        assert transport.calls == 0
        assert transport.send_entries == 0
        assert coordinator._permits._issued == {}
        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.investigation_id == investigation_id)
            )
        assert count == 0
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            service = InvestigationService(
                post_synthesis=None,
                investigations=uow.analyst_investigations,
                states=uow.investigation_states,
                context_snapshots=None,
                envelopes=uow.investigation_envelopes,
                context_builder=None,
                bridge=None,
                registry=None,
                dispatcher=None,
                invocations=uow.tool_invocations,
                observations=uow.evidence_observations,
                findings=uow.investigation_findings,
                opinions=uow.analyst_opinions,
                commit_uow=uow.commit,
            )
            with pytest.raises(ValueError, match="Ambiguous tool invocations"):
                await service.resume(investigation_id)
        async with session_factory() as session:
            invocation = await session.get(ToolInvocationRow, authority.invocation_id)
            reservation = await session.get(
                InvestigationBudgetReservationRow,
                (investigation_id, authority.operation_key),
            )
            amount = await session.get(
                InvestigationBudgetReservationAmountRow,
                (investigation_id, authority.operation_key, "vt_units"),
            )
        assert invocation is not None and invocation.status == "needs_review"
        assert reservation is not None and reservation.status == "reserved"
        assert amount is not None and amount.reserved_amount == 1
        assert amount.committed_amount is None
        assert transport.calls == 0
        assert transport.send_entries == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_crash_after_consume_seals_review_and_replay_never_sends(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    transport = _CountingTransport()
    try:
        consumed = await store.consume(
            authority,
            prepared,
            effect_key=effect_key(investigation_id, prepared),
            occurrence_digest=occurrence_sha256(prepared.occurrence_id),
        )
        assert isinstance(consumed, ConsumeCommitted)
        # Simulate restart recovery after the consume commit and before handoff.
        assert await store.needs_review(consumed.claim_id, reason_code="worker_crash_after_consume")
        replay = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert isinstance(replay, Ambiguous)
        assert transport.calls == 0
        async with session_factory() as session:
            claim = await session.get(ExternalEffectClaimRow, consumed.claim_id)
            reservation = await session.get(
                InvestigationBudgetReservationRow,
                (investigation_id, authority.operation_key),
            )
            amount = await session.get(
                InvestigationBudgetReservationAmountRow,
                (investigation_id, authority.operation_key, "vt_units"),
            )
        assert claim is not None and claim.state == "NEEDS_REVIEW"
        assert reservation is not None and reservation.status == "reserved"
        assert (
            amount is not None and amount.reserved_amount == 1 and amount.committed_amount is None
        )
    finally:
        await engine.dispose()

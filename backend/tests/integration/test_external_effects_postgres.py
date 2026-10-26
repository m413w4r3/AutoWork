from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.external_effects import (
    Ambiguous,
    Completed,
    ConsumeCommitted,
    ConsumePolicyContext,
    DefinitelyNotSubmitted,
    ExternalEffectCoordinator,
    HandoffGateRefused,
    PhysicalSendCapability,
    Refused,
    Reused,
    ReuseDecision,
    TransportResponse,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.external_effects import (
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
    BudgetDimension,
    InvestigationAgenda,
    InvestigationBudget,
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
from cti_app.infrastructure.database.repositories.investigation import _budget_to_payload
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.workflow_emulator.investigation_support import seed_verified_article

pytestmark = pytest.mark.integration

_FAMILY = "vt.file_report"
_PROVIDER = "virustotal"
_NORMALIZER = "vt.file-report.v1"
_ARGUMENTS = {"hash": "abc"}
_REQUEST = {"hash": "abc", "include": ["relations", "history"]}


class _CountingTransport:
    def __init__(self) -> None:
        self.__raw_client = _CountingRawClient()
        self.submit_entries = 0

    @property
    def calls(self) -> int:
        return self.__raw_client.calls

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
        if self.__raw_client.retries_enabled:
            raise HandoffGateRefused("raw client retries must be disabled")
        capability(request)
        return await self.__raw_client.send(request, follow_redirects=False)


class _CountingRawClient:
    def __init__(self) -> None:
        self.calls = 0
        self.retries_enabled = False

    async def send(
        self, _request: FrozenWireRequest, *, follow_redirects: bool
    ) -> TransportResponse:
        assert follow_redirects is False
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
    """Boundedly rendezvous workers immediately before the claim transaction."""

    def __init__(
        self,
        delegate: SqlAlchemyExternalEffectClaimStore,
        barrier: asyncio.Barrier,
        *,
        timeout_seconds: float = 5.0,
    ):
        self._delegate = delegate
        self._barrier = barrier
        self._timeout_seconds = timeout_seconds
        self._participants_remaining = barrier.parties
        self._barrier_aborted = False
        self._barrier_state_lock = asyncio.Lock()

    async def consume(self, *args: Any, **kwargs: Any):
        async with self._barrier_state_lock:
            should_wait = not self._barrier_aborted and self._participants_remaining > 0
            if should_wait:
                self._participants_remaining -= 1
        if not should_wait:
            return await self._delegate.consume(*args, **kwargs)
        try:
            await asyncio.wait_for(self._barrier.wait(), timeout=self._timeout_seconds)
        except TimeoutError as exc:
            # A caller may have refused during coordinator pre-validation and
            # never reached this seam. Break the one-shot barrier so no peer
            # waits on it; later replay calls bypass this already-used gate.
            async with self._barrier_state_lock:
                self._barrier_aborted = True
            await self._barrier.abort()
            raise RuntimeError("claim workers did not rendezvous before timeout") from exc
        except asyncio.BrokenBarrierError as exc:
            async with self._barrier_state_lock:
                self._barrier_aborted = True
            raise RuntimeError("claim worker barrier was aborted") from exc
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
    budget = InvestigationBudget(limits={BudgetDimension.VT_UNITS: max(1, invocations)})
    operation_keys = [f"external-effect-op-{uuid4()}" for _ in range(invocations)]
    for operation_key in operation_keys:
        budget.reserve(operation_key, {BudgetDimension.VT_UNITS: 1})
    budget_snapshot = _budget_to_payload(budget)
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
                    budget_snapshot=budget_snapshot,
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
                        "budget_snapshot": budget_snapshot,
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
                operation_key = operation_keys[index]
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


@pytest.mark.asyncio
async def test_sql_store_seals_real_pretransport_failure_and_rejects_forged_proofs(
    migrated_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    transport = _CountingTransport()
    external_effects = importlib.import_module("cti_app.application.external_effects")
    ticks = iter((10.0, 12.0))
    monkeypatch.setattr(
        external_effects, "time", SimpleNamespace(monotonic=lambda: next(ticks, 12.0))
    )
    original_seal = store.definitely_not_submitted
    captured_proofs: list[object] = []

    async def check_untrusted_proofs_before_seal(
        claim_id: UUID, *, proof: object, reason_code: str
    ) -> bool:
        captured_proofs.append(proof)
        assert not await original_seal(uuid4(), proof=proof, reason_code="wrong_claim")
        assert not await original_seal(claim_id, proof=copy.copy(proof), reason_code="copied_proof")
        assert not await original_seal(claim_id, proof=object(), reason_code="forged_proof")
        return await original_seal(claim_id, proof=proof, reason_code=reason_code)

    monkeypatch.setattr(store, "definitely_not_submitted", check_untrusted_proofs_before_seal)
    coordinator = ExternalEffectCoordinator(store)
    try:
        outcome = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(outcome, DefinitelyNotSubmitted)
        assert transport.submit_entries == 0
        async with session_factory() as session:
            row = await session.get(ExternalEffectClaimRow, outcome.claim_id)
        assert row is not None
        assert row.state == "DEFINITIVELY_NOT_SUBMITTED"
        assert row.handoff_permit_nonce is None
        assert row.investigation_id == investigation_id
        assert len(captured_proofs) == 1
        assert not await original_seal(
            outcome.claim_id, proof=captured_proofs[0], reason_code="replayed_proof"
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_second_coordinator_cannot_seal_claim_with_foreign_proof(
    migrated_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_factory, engine, _investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    _coordinator = ExternalEffectCoordinator(store)
    consumed = await store.consume(
        authority,
        prepared,
        effect_key=effect_key(authority.investigation_id, prepared),
        occurrence_digest=occurrence_sha256(prepared.occurrence_id),
    )
    assert isinstance(consumed, ConsumeCommitted), "fixture must create a committed claim"

    foreign_store = _store(session_factory)
    external_effects = importlib.import_module("cti_app.application.external_effects")
    ticks = iter((20.0, 22.0))
    monkeypatch.setattr(
        external_effects, "time", SimpleNamespace(monotonic=lambda: next(ticks, 22.0))
    )

    class _ClaimReplayProbe:
        def __getattr__(self, name: str) -> Any:
            return getattr(foreign_store, name)

        async def consume(self, *args: Any, **kwargs: Any) -> ConsumeCommitted:
            assert isinstance(consumed, ConsumeCommitted), (
                "claim replay probe requires a committed consume result"
            )
            return consumed

        async def needs_review(self, claim_id: UUID, *, reason_code: str) -> bool:
            # Keep the durable row CONSUMED so this test isolates rejection
            # before the definitive-not-submitted CAS.
            return True

        async def definitely_not_submitted(
            self, claim_id: UUID, *, proof: object, reason_code: str
        ) -> bool:
            return await store.definitely_not_submitted(
                claim_id, proof=proof, reason_code=reason_code
            )

    foreign_coordinator = ExternalEffectCoordinator(_ClaimReplayProbe())  # type: ignore[arg-type]
    transport = _CountingTransport()
    try:
        outcome = await foreign_coordinator.send_one(authority, prepared, transport)
        assert isinstance(outcome, Ambiguous)
        assert outcome.claim_id == consumed.claim_id
        assert transport.submit_entries == 0
        async with session_factory() as session:
            row = await session.get(ExternalEffectClaimRow, consumed.claim_id)
        assert row is not None and row.state == "CONSUMED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_refuses_committed_external_effect_rows(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    authority, prepared = authorities[0]
    store = _store(session_factory)
    try:
        consumed = await store.consume(
            authority,
            prepared,
            effect_key=effect_key(investigation_id, prepared),
            occurrence_digest=occurrence_sha256(prepared.occurrence_id),
        )
        assert isinstance(consumed, ConsumeCommitted)
        with pytest.raises(RuntimeError, match="refusing to drop external effect"):
            command.downgrade(_alembic_config(migrated_postgres_url), "0003_osint_open_policy")
        async with session_factory() as session:
            claim = await session.get(ExternalEffectClaimRow, consumed.claim_id)
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert claim is not None and claim.state == "CONSUMED"
        assert version == "0004_external_effect_claims"
    finally:
        await engine.dispose()


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
        completed_result = next(result for result in results if isinstance(result, Completed))
        assert sum(isinstance(result, Completed) for result in results) == 1
        assert sum(isinstance(result, (Refused, Ambiguous)) for result in results) == 1
        first_authority, first_prepared = authorities[0]
        replay = await coordinator.send_one(first_authority, first_prepared, transport)
        assert isinstance(replay, Refused)
        assert replay.reason == "effect_occurrence_required"
        assert transport.calls == 1
        async with session_factory() as session:
            completed_claim = await session.get(ExternalEffectClaimRow, completed_result.claim_id)
            first_reservation = await session.get(
                InvestigationBudgetReservationRow,
                (investigation_id, first_authority.operation_key),
            )
            envelope = await session.get(InvestigationEnvelopeRow, investigation_id)
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
        assert completed_claim is not None
        assert first_reservation is not None
        assert envelope is not None
        first_budget_reservation = next(
            item
            for item in envelope.budget_snapshot["reservations"]
            if item["operation_key"] == first_authority.operation_key
        )
        if first_authority.invocation_id == completed_claim.invocation_id:
            assert first_reservation.status == "reserved"
            assert first_budget_reservation["status"] == "reserved"
            assert envelope.version == 4
        else:
            assert first_reservation.status == "released"
            assert first_budget_reservation["status"] == "released"
            assert envelope.version == 5
        assert claim_count == 1
        assert sent_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_new_occurrence_creates_claim_after_recorded_reuse(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, authorities = await _prepare_case(
        migrated_postgres_url,
        tmp_path,
        suffix=uuid4().hex[:12],
        invocations=3,
    )

    class _AllowReuse:
        async def authorize_reuse(
            self,
            claim: Any,
            _authority: ExternalEffectAuthority,
            _prepared: PreparedExternalRequest,
        ) -> ReuseDecision:
            return ReuseDecision(source_invocation_id=claim.invocation_id, observation_refs=())

    coordinator = ExternalEffectCoordinator(
        _store(session_factory), reuse_policies={_FAMILY: _AllowReuse()}
    )
    transport = _CountingTransport()
    original_authority, original_prepared = authorities[0]
    try:
        original = await coordinator.send_one(original_authority, original_prepared, transport)
        assert isinstance(original, Completed)
        assert transport.calls == 1

        reuse_authority, reuse_prepared = authorities[1]
        reused = await coordinator.send_one(
            replace(reuse_authority, expected_envelope_version=5),
            reuse_prepared,
            transport,
        )
        assert isinstance(reused, Reused)
        assert reused.claim_id == original.claim_id
        assert transport.calls == 1

        occurrence_authority, _ = authorities[2]
        new_prepared = replace(original_prepared, occurrence_id=uuid4())
        new_effect = await coordinator.send_one(
            replace(occurrence_authority, expected_envelope_version=5),
            new_prepared,
            transport,
        )
        assert isinstance(new_effect, Completed)
        assert new_effect.claim_id != original.claim_id
        assert transport.calls == 2

        async with session_factory() as session:
            original_claim = await session.get(ExternalEffectClaimRow, original.claim_id)
            new_claim = await session.get(ExternalEffectClaimRow, new_effect.claim_id)
            claims = await session.scalar(
                select(func.count())
                .select_from(ExternalEffectClaimRow)
                .where(ExternalEffectClaimRow.investigation_id == investigation_id)
            )
            original_uses = list(
                (
                    await session.scalars(
                        select(ExternalEffectUseRow).where(
                            ExternalEffectUseRow.claim_id == original.claim_id
                        )
                    )
                ).all()
            )
            new_uses = list(
                (
                    await session.scalars(
                        select(ExternalEffectUseRow).where(
                            ExternalEffectUseRow.claim_id == new_effect.claim_id
                        )
                    )
                ).all()
            )
        assert original_claim is not None and original_claim.state == "COMPLETED"
        assert new_claim is not None and new_claim.state == "COMPLETED"
        assert claims == 2
        assert sorted(use.use_kind for use in original_uses) == ["REUSED", "SENT"]
        assert [use.use_kind for use in new_uses] == ["SENT"]
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
    coordinator = ExternalEffectCoordinator(failing_store)  # type: ignore[arg-type]
    try:
        outcome = await coordinator.send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "consume_unavailable"
        assert failing_store.consume_calls == 1
        assert transport.calls == 0
        assert transport.submit_entries == 0
        assert "_permits" not in dir(coordinator)
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
        assert transport.submit_entries == 0
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

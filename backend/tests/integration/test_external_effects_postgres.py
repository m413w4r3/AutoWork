from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.external_effects import (
    Ambiguous,
    Completed,
    ConsumeCommitted,
    ConsumePolicyContext,
    ExternalEffectCoordinator,
    Refused,
    TransportResponse,
)
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
        self._lock = asyncio.Lock()

    async def send(
        self, request: FrozenWireRequest, *, permit: ConsumedEffectPermit
    ) -> TransportResponse:
        async with self._lock:
            self.calls += 1
        return TransportResponse(result_ref="artifact:pg-fake-result")


class _DenyPolicyGuard:
    def __init__(self) -> None:
        self.called_under_transaction = False

    async def authorize(self, context: ConsumePolicyContext) -> bool:
        self.called_under_transaction = bool(context.database_session.in_transaction())
        return False


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
    uow_factory = SqlAlchemyUnitOfWork(session_factory)
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / f"external-effects-{suffix}"))
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
    return session_factory, engine, seeded.investigation.id, subject_id, authorities


def update_investigation_status(investigation_id: UUID):
    from sqlalchemy import update

    return (
        update(AnalystInvestigationRow)
        .where(AnalystInvestigationRow.id == investigation_id)
        .values(status="running")
    )


def _store(
    session_factory: async_sessionmaker[AsyncSession], *, policy_guard=None
) -> SqlAlchemyExternalEffectClaimStore:
    return SqlAlchemyExternalEffectClaimStore(
        session_factory,
        policy_guard=policy_guard,
        provider_bindings={_FAMILY: (_PROVIDER, frozenset({"tenant-test"}))},
        normalizer_versions={_FAMILY: frozenset({_NORMALIZER})},
        invocation_bindings={_FAMILY: (_FAMILY, "v1")},
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
    coordinator = ExternalEffectCoordinator(store)
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


@pytest.mark.asyncio
async def test_revocation_first_guard_denies_with_zero_send(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, subject_id, authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    base, prepared = authorities[0]
    authority = ExternalEffectAuthority(
        investigation_id=base.investigation_id,
        invocation_id=base.invocation_id,
        operation_key=base.operation_key,
        reservation_amounts=base.reservation_amounts,
        expected_envelope_version=base.expected_envelope_version,
        approval_decision_id=base.approval_decision_id,
        invocation_arguments_sha256=base.invocation_arguments_sha256,
        authority_kind=ExternalEffectAuthorityKind.OSINT_POLICY,
        policy_event_id=uuid4(),
        policy_version="osint-open.v1",
        pinned_subject_id=subject_id,
    )
    guard = _DenyPolicyGuard()
    store = _store(session_factory, policy_guard=guard)
    transport = _CountingTransport()
    try:
        outcome = await ExternalEffectCoordinator(store).send_one(authority, prepared, transport)
        assert isinstance(outcome, Refused)
        assert outcome.reason == "osint_policy_denied"
        assert guard.called_under_transaction
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
async def test_crash_before_consume_leaves_no_claim(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    session_factory, engine, investigation_id, _subject_id, _authorities = await _prepare_case(
        migrated_postgres_url, tmp_path, suffix=uuid4().hex[:12]
    )
    try:
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

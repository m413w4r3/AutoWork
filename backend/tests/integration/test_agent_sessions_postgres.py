"""PostgreSQL oracles for migration 0023 and the agent-session constraints.

Disposable databases only: the created tables/indexes, the partial unique
active-session index under a real race, the idempotency uniques, the tool
invocation attribution pair, the instruction checks, the task lease foreign
key, the ADR-0007 preflight and the non-empty downgrade guard.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from fastapi import FastAPI
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.api.agent_sessions import router as agent_sessions_router
from cti_app.application.agent_session_verification import (
    HmacSessionSecretDeriver,
    build_agent_session_message,
)
from cti_app.application.agentic_host_sessions import (
    HostBudgetUsageV1,
    HostCloseResultV1,
    HostInterruptResultV1,
    HostReasonRequestV1,
    HostSessionsPort,
    HostSessionStatusV1,
)
from cti_app.application.agentic_sessions import (
    HostSessionReleaseService,
    SessionSecret,
    SessionService,
    SessionStateConflictError,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_handoffs import (
    HandoffCreateRequest,
    HandoffTaskStaleError,
    InvestigationHandoffService,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_task_sweep import recover_orphan_agent_sessions
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    RepositoryTaskSessionPort,
    TaskCommandError,
)
from cti_app.config import Settings
from cti_app.domain.agentic_operator import (
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionV1,
    CollectResultsRequest,
    HandoffRequestReceiptV1,
    InstructionDeliveryMode,
    InstructionReceiptV1,
    InstructionState,
    InterruptSessionRequest,
    InterruptSessionResultV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorConfinementV1,
    OperatorProbeCommand,
    OperatorResultsV1,
    PreparedOperatorSessionV1,
    PrepareSessionRequest,
    ProbeCapabilitiesRequest,
    ReconcileSessionRequest,
    RequestHandoffRequest,
    ResumeSessionRequest,
    ResumeSessionResultV1,
    SendInstructionRequest,
    SessionProcessStatusV1,
    SessionReconcileOutcome,
    SessionReconciliationV1,
    SessionStatusRequest,
    StartSessionRequest,
    instruction_text_sha256,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation_handoff import HandoffActor, HandoffActorKind
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskLimits,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.database.models.investigation import ToolInvocationRow
from cti_app.infrastructure.database.repositories.agent_sessions import (
    SqlAlchemyAgentSessionRepository,
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.alembic_head import current_head
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

PREVIOUS = "0022_investigation_handoffs"
HEAD = current_head()
SESSIONS = "agent_sessions"
INSTRUCTIONS = "agent_session_instructions"
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
HUMAN_ACTOR = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")


def _table_names(database_url: str) -> set[str]:
    async def run() -> set[str]:
        engine = create_postgres_engine(database_url)
        try:
            async with engine.connect() as connection:
                return set(
                    await connection.run_sync(
                        lambda sync_connection: sa.inspect(sync_connection).get_table_names()
                    )
                )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _alembic_version(database_url: str) -> str:
    async def run() -> str:
        engine = create_postgres_engine(database_url)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                assert isinstance(value, str)
                return value
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Agent session fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Agent session fixture",
            slug=f"agent-session-fixture-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="e" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(),
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


async def _seed_task(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    *,
    task_key: str,
) -> InvestigationTaskV1:
    task = InvestigationTaskV1(
        investigation_id=investigation_id,
        task_key=task_key,
        title="Inspect the loader",
        question="Which network gate does the loader use?",
        objective="Identify the first-stage C2 gate.",
        priority=TaskPriority.HIGH,
        status=TaskStatus.READY,
        status_version=1,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
        created_at=NOW,
        updated_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored = await uow.investigation_tasks.add(task)
        await uow.commit()
    return stored


async def _seed_handoff(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    task_id: UUID,
) -> UUID:
    service = InvestigationHandoffService(uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory))
    record = await service.create(
        investigation_id,
        actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
        idempotency_key="session-fixture-handoff",
        request=HandoffCreateRequest(
            task_id=task_id,
            to_actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id="codex:operator-1"),
            next_action="Continue the analysis.",
        ),
    )
    return UUID(record.handoff.handoff_id)


async def _allow_disclosure(investigation_id: UUID) -> None:
    """This schema/race oracle is not about the disclosure gate."""
    return None


def _capabilities() -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable="/usr/local/bin/codex",
        version="codex-cli 1.2.3",
        modes=("exec",),
        supports_resume=True,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        permission_modes=("workspace-write",),
        confinement=OperatorConfinementV1(
            profile="bwrap-reasoning-session",
            bwrap_ok=True,
            netns=True,
            network_mode="isolated_egress",
            loopback_isolated=True,
        ),
        probed_at=NOW,
        probes=(
            OperatorProbeCommand(
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "resume", "workspace-write"),
            ),
        ),
    )


def _session(
    investigation_id: UUID,
    task_id: UUID,
    handoff_id: UUID,
    *,
    idempotency_key: str,
    state: AgentSessionState = AgentSessionState.RUNNING,
) -> AgentSessionV1:
    session_id = uuid4()
    nonce = b"s" * 32
    capabilities = _capabilities()
    return AgentSessionV1(
        id=session_id,
        investigation_id=investigation_id,
        task_id=task_id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=state,
        state_version=2,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=nonce,
        auth_secret_hash=hashlib.sha256(nonce).hexdigest(),
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=idempotency_key,
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
        handoff_id=handoff_id,
    )


def _invocation_values(
    investigation_id: UUID,
    *,
    task_id: UUID | None,
    agent_session_id: UUID | None,
) -> dict[str, object]:
    return {
        "id": uuid4(),
        "investigation_id": investigation_id,
        "operation_key": "op-" + uuid4().hex,
        "tool_name": "advisor.consult",
        "schema_version": "advisor-consult.v1",
        "normalized_parameters": {},
        "purpose": "attribution fixture",
        "task_id": task_id,
        "agent_session_id": agent_session_id,
        "actor_kind": "operator" if task_id is not None else None,
        "actor_id": "codex:operator-1" if task_id is not None else None,
        "status": "pending",
        "reserved_budget": {},
        "consumed_budget": {},
        "response_refs": [],
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }


async def _insert_invocation(
    session_factory: async_sessionmaker[AsyncSession],
    values: dict[str, object],
) -> None:
    async with session_factory() as session:
        await session.execute(sa.insert(ToolInvocationRow).values(**values))
        await session.commit()


async def _sqlstate_for(statement: sa.Executable, params: dict[str, object], url: str) -> str:
    """Run one statement in its own transaction; return the SQLSTATE or 'ok'."""
    engine = create_postgres_engine(url)
    try:
        try:
            async with engine.begin() as connection:
                await connection.execute(statement, params)
        except DBAPIError as exc:
            return str(getattr(exc.orig, "sqlstate", ""))
        return "ok"
    finally:
        await engine.dispose()


async def _sqlstate_for_invocation(values: dict[str, object], url: str) -> str:
    """Insert one raw invocation row; the domain pair check is bypassed."""
    return await _sqlstate_for(sa.insert(ToolInvocationRow).values(**values), {}, url)


def test_agent_session_migration_and_single_active_race(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    tables = _table_names(temporary_postgres_url)
    assert SESSIONS in tables and INSTRUCTIONS in tables
    assert _alembic_version(temporary_postgres_url) == HEAD

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-race")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            first = _session(investigation.id, task.id, handoff_id, idempotency_key="race-a")
            second = _session(investigation.id, task.id, handoff_id, idempotency_key="race-b")

            async def insert(session_row: AgentSessionV1) -> object:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    stored, created = await uow.agent_sessions.add(session_row)
                    await uow.commit()
                    return stored, created

            outcomes = await asyncio.gather(insert(first), insert(second), return_exceptions=True)
            successes = [item for item in outcomes if not isinstance(item, BaseException)]
            failures = [item for item in outcomes if isinstance(item, VersionConflictError)]
            assert len(successes) == 1
            assert len(failures) == 1

            async with SqlAlchemyUnitOfWork(factory) as uow:
                same_key, created = await uow.agent_sessions.add(
                    _session(
                        investigation.id,
                        task.id,
                        handoff_id,
                        idempotency_key="race-a",
                    )
                )
                # The same key replays whichever row won key ``race-a``.
                assert created is False
                assert same_key.task_id == task.id
                sessions = await uow.agent_sessions.list_for_task(task.id)
                assert len(sessions) == 1
                assert sessions[0].state is AgentSessionState.RUNNING
                stale = sessions[0]
            async with engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "UPDATE agent_sessions SET state_version = state_version + 1 WHERE id = :id"
                    ),
                    {"id": stale.id},
                )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                updated = stale.with_last_activity(now=NOW)
                conflict = await uow.agent_sessions.update_state(
                    updated, expected_state_version=stale.state_version
                )
                assert conflict is None
        finally:
            await engine.dispose()

    asyncio.run(run())

    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, PREVIOUS)
    assert _alembic_version(temporary_postgres_url) == HEAD


def test_tool_invocation_attribution_pair_and_lease_fk(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-pair")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            session_row = _session(
                investigation.id, task.id, handoff_id, idempotency_key="pair-session"
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.agent_sessions.add(session_row)
                await uow.commit()

            # A task-only invocation and a session-only invocation both violate
            # the cumulative attribution pair.
            assert (
                await _sqlstate_for_invocation(
                    _invocation_values(investigation.id, task_id=task.id, agent_session_id=None),
                    temporary_postgres_url,
                )
                == "23514"
            )
            assert (
                await _sqlstate_for_invocation(
                    _invocation_values(
                        investigation.id,
                        task_id=None,
                        agent_session_id=session_row.id,
                    ),
                    temporary_postgres_url,
                )
                == "23514"
            )

            # The full pair persists, and an unknown session id is an FK error.
            await _insert_invocation(
                factory,
                _invocation_values(
                    investigation.id,
                    task_id=task.id,
                    agent_session_id=session_row.id,
                ),
            )
            assert (
                await _sqlstate_for_invocation(
                    _invocation_values(investigation.id, task_id=task.id, agent_session_id=uuid4()),
                    temporary_postgres_url,
                )
                == "23503"
            )

            # A complete lease naming an unknown session is refused by the D2 FK
            # (setting only the session column would trip the pair check first).
            assert (
                await _sqlstate_for(
                    sa.text(
                        "UPDATE investigation_tasks SET lease_id = :lease_id, "
                        "lease_session_id = :session_id, lease_fence = 1, "
                        "heartbeat_at = :now, lease_expires_at = :later "
                        "WHERE id = :task_id"
                    ),
                    {
                        "lease_id": uuid4(),
                        "session_id": uuid4(),
                        "now": NOW,
                        "later": datetime(2026, 11, 22, 12, 5, tzinfo=UTC),
                        "task_id": task.id,
                    },
                    temporary_postgres_url,
                )
                == "23503"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_agent_session_instructions_unique_and_state_checks(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-instr")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            session_row = _session(
                investigation.id, task.id, handoff_id, idempotency_key="instr-session"
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.agent_sessions.add(session_row)
                await uow.commit()

            async def insert_instruction(**overrides: object) -> str:
                values: dict[str, object] = {
                    "id": uuid4(),
                    "investigation_id": investigation.id,
                    "task_id": task.id,
                    "session_id": session_row.id,
                    "idempotency_key": "instruction-key",
                    "body": "Check the gate.",
                    "body_sha256": hashlib.sha256(b"Check the gate.").hexdigest(),
                    "state": "queued",
                    "created_by_actor_kind": "human",
                    "created_by_actor_id": "analyst:test",
                    "queued_at": NOW,
                    "delivered_at": None,
                    "acknowledged_at": None,
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                values.update(overrides)
                return await _sqlstate_for(
                    sa.text(
                        "INSERT INTO agent_session_instructions ("
                        "id, investigation_id, task_id, session_id, idempotency_key, "
                        "body, body_sha256, state, created_by_actor_kind, "
                        "created_by_actor_id, queued_at, delivered_at, "
                        "acknowledged_at, created_at, updated_at) VALUES ("
                        ":id, :investigation_id, :task_id, :session_id, "
                        ":idempotency_key, :body, :body_sha256, :state, "
                        ":created_by_actor_kind, :created_by_actor_id, :queued_at, "
                        ":delivered_at, :acknowledged_at, :created_at, :updated_at)"
                    ),
                    values,
                    temporary_postgres_url,
                )

            assert await insert_instruction() == "ok"
            assert await insert_instruction(id=uuid4()) == "23505"
            assert (
                await insert_instruction(
                    id=uuid4(),
                    idempotency_key="instruction-2",
                    state="acknowledged",
                )
                == "23514"
            )
            assert (
                await insert_instruction(
                    id=uuid4(),
                    idempotency_key="instruction-3",
                    session_id=uuid4(),
                )
                == "23503"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_agent_session_migration_accepts_conforming_and_rejects_partial(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    # Stop at 0023 before stamping back: 0027 later replaces three of the
    # constraints this revision owns, so 0023 only accepts its own shape.
    command.upgrade(config, "0023_agent_sessions")
    command.stamp(config, PREVIOUS)
    command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == HEAD
    assert SESSIONS in _table_names(temporary_postgres_url)

    async def drop_instructions() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(sa.text(f"DROP TABLE {INSTRUCTIONS}"))
        finally:
            await engine.dispose()

    asyncio.run(drop_instructions())
    command.stamp(config, PREVIOUS)
    with pytest.raises(RuntimeError, match="preflight rejected"):
        command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == PREVIOUS


class _BlockingOperator:
    """A real port fake whose start blocks until the test releases it."""

    adapter_kind = OperatorAdapterKind.CODEX_CLI

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted: list[UUID] = []
        self.manifests: list[UUID] = []

    async def probe_capabilities(self, request: ProbeCapabilitiesRequest) -> OperatorCapabilitiesV1:
        return _capabilities()

    async def prepare_session(self, request: PrepareSessionRequest) -> PreparedOperatorSessionV1:
        return PreparedOperatorSessionV1(
            session_id=request.session_id,
            investigation_id=request.investigation_id,
            task_id=request.task_id,
            adapter_kind=request.adapter_kind,
            host_session_ref=f"host-{request.session_id}",
            capabilities=_capabilities(),
            prepared_at=NOW,
        )

    async def start_session(self, request: StartSessionRequest) -> SessionProcessStatusV1:
        self.entered.set()
        await self.release.wait()
        return SessionProcessStatusV1(
            session_id=request.session_id,
            host_session_ref=request.host_session_ref,
            alive=True,
            last_activity_at=NOW,
            observed_at=NOW,
        )

    async def set_tool_manifest(
        self, session_id: UUID, *, host_session_ref: str, capability_sha256: str
    ) -> None:
        self.manifests.append(session_id)

    async def interrupt_session(self, request: InterruptSessionRequest) -> InterruptSessionResultV1:
        self.interrupted.append(request.session_id)
        return InterruptSessionResultV1(
            session_id=request.session_id, stopped=False, observed_at=NOW
        )

    async def get_session_status(self, request: SessionStatusRequest) -> SessionProcessStatusV1:
        raise AssertionError("the interleaving test never polls the adapter")

    async def send_instruction(self, request: SendInstructionRequest) -> InstructionReceiptV1:
        raise AssertionError("the interleaving test never sends an instruction")

    async def request_handoff(self, request: RequestHandoffRequest) -> HandoffRequestReceiptV1:
        raise AssertionError("the interleaving test never requests a handoff")

    async def resume_session(self, request: ResumeSessionRequest) -> ResumeSessionResultV1:
        raise AssertionError("the interleaving test never resumes")

    async def collect_results(self, request: CollectResultsRequest) -> OperatorResultsV1:
        raise AssertionError("the interleaving test never collects")

    async def reconcile_session(self, request: ReconcileSessionRequest) -> SessionReconciliationV1:
        raise AssertionError("the interleaving test never reconciles")


class _HostIoTrackingUowFactory:
    """Counts open service UoWs so a blocked fake can assert the host boundary."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.active = 0

    def __call__(self) -> _HostIoTrackingUow:
        return _HostIoTrackingUow(self)


class _HostIoTrackingUow:
    def __init__(self, owner: _HostIoTrackingUowFactory) -> None:
        self.owner = owner
        self.inner: SqlAlchemyUnitOfWork | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self.inner = SqlAlchemyUnitOfWork(self.owner.session_factory)
        await self.inner.__aenter__()
        self.owner.active += 1
        return self.inner

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self.inner is not None
        try:
            await self.inner.__aexit__(exc_type, exc_value, traceback)
        finally:
            self.owner.active -= 1


class _BlockingCollectOperator(_BlockingOperator):
    """Deterministic collect UDS fake; it never starts a real CLI."""

    def __init__(self, uow_depth: Callable[[], int]) -> None:
        super().__init__()
        self.uow_depth = uow_depth
        self.uow_depth_at_call: int | None = None
        self.collected: list[UUID] = []
        self.both_entered = asyncio.Event()

    async def collect_results(self, request: CollectResultsRequest) -> OperatorResultsV1:
        self.uow_depth_at_call = self.uow_depth()
        self.collected.append(request.session_id)
        if len(self.collected) >= 2:
            self.both_entered.set()
        self.entered.set()
        await self.release.wait()
        return OperatorResultsV1(
            session_id=request.session_id,
            checkpoint_ref="checkpoint-after-collect",
            checkpoint_sha256="c" * 64,
            observed_at=NOW,
        )

    async def reconcile_session(self, request: ReconcileSessionRequest) -> SessionReconciliationV1:
        return SessionReconciliationV1(
            session_id=request.session_id,
            outcome=SessionReconcileOutcome.SESSION_ALIVE,
            host_session_ref=request.host_session_ref,
            observed_at=NOW,
        )


class _SecretDelivery:
    def __init__(self) -> None:
        self.delivered: list[UUID] = []

    async def deliver_session_secret(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        adapter_kind: OperatorAdapterKind,
        host_session_ref: str,
        secret: SessionSecret,
        idempotency_key: str,
    ) -> None:
        self.delivered.append(session_id)


def test_open_session_holds_no_lock_during_the_host_start(
    temporary_postgres_url: str,
) -> None:
    """B3 interleaving: a human cancel wins while the host start is in flight.

    Before the lock-order fix, ``open_session`` held the task row across the
    adapter call, so this cancel blocked (and could deadlock against the
    advisory lock).  The fix commits the provisioning row first, starts the
    host without locks, then acquires the lease in a second short transaction.
    """
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-interleave")
            async with SqlAlchemyUnitOfWork(factory) as uow:
                engine_service = InvestigationTaskService(
                    tasks=uow.investigation_tasks,
                    decisions=uow.analyst_decisions,
                    sessions=RepositoryTaskSessionPort(uow.agent_sessions),
                    limits=TaskLimits(),
                )
                assigned = await engine_service.assign(
                    task_id=task.id,
                    actor=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
                    target=TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1"),
                    expected_status_version=1,
                    idempotency_key="assign-interleave",
                    now=NOW,
                )
                await uow.commit()
            assert assigned.task.status is TaskStatus.ASSIGNED
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)

            operator = _BlockingOperator()
            service = SessionService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                deriver=HmacSessionSecretDeriver("0123456789abcdef0123456789abcdef"),
                operator=operator,
                secret_delivery=_SecretDelivery(),
                disclosure=_allow_disclosure,
                task_limits=TaskLimits(),
                clock=lambda: NOW,
            )
            opening = asyncio.create_task(
                service.open_session(
                    investigation.id,
                    task_id=task.id,
                    adapter_kind=OperatorAdapterKind.CODEX_CLI,
                    handoff_id=handoff_id,
                    executable="/usr/local/bin/codex",
                    actor=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
                    idempotency_key="open-interleave",
                    now=NOW,
                )
            )
            await asyncio.wait_for(operator.entered.wait(), timeout=10)

            async def cancel() -> None:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    cancel_service = InvestigationTaskService(
                        tasks=uow.investigation_tasks,
                        decisions=uow.analyst_decisions,
                        sessions=RepositoryTaskSessionPort(uow.agent_sessions),
                        limits=TaskLimits(),
                    )
                    await cancel_service.cancel(
                        task_id=task.id,
                        actor=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
                        expected_status_version=assigned.task.status_version,
                        idempotency_key="cancel-interleave",
                        reason="stop now",
                        now=NOW,
                    )
                    await uow.commit()

            # The host start is still blocked: cancel must not wait for it.
            assert not operator.release.is_set()
            await asyncio.wait_for(cancel(), timeout=10)
            assert not opening.done()

            operator.release.set()
            with pytest.raises((TaskCommandError, HandoffTaskStaleError)):
                await asyncio.wait_for(opening, timeout=10)
            assert operator.interrupted, "the failed activation must stop the host run"

            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.investigation_tasks.get(task.id)
                assert stored is not None
                assert stored.status is TaskStatus.CANCELLED
                sessions = await uow.agent_sessions.list_for_task(task.id)
                assert len(sessions) == 1
                assert sessions[0].state is AgentSessionState.FAILED
        finally:
            await engine.dispose()

    asyncio.run(run())


class _ClosingHost:
    """The real host port method the release service needs; records the calls."""

    def __init__(self) -> None:
        self.closed: list[UUID] = []

    async def close_session(
        self, session_id: UUID, request: HostReasonRequestV1, *, idempotency_key: str
    ) -> HostCloseResultV1:
        self.closed.append(session_id)
        return HostCloseResultV1(state="closed")

    async def interrupt_session(
        self, session_id: UUID, request: HostReasonRequestV1, *, idempotency_key: str
    ) -> HostInterruptResultV1:
        return HostInterruptResultV1(state="interrupt_requested", signal_sent=True)

    async def get_session_status(self, session_id: UUID) -> HostSessionStatusV1:
        return HostSessionStatusV1(
            state="closed",
            state_version=1,
            adapter_kind="codex_cli",
            budgets_used=HostBudgetUsageV1(wall_seconds=0, output_bytes=0, instructions=0),
            last_activity_at=NOW,
            log_cursor=0,
        )


def test_crash_orphan_is_failed_and_the_task_becomes_openable(
    temporary_postgres_url: str,
) -> None:
    """BLOCKER 1(b): the sweep recovers a session a crash left without a lease."""
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-orphan")
            async with SqlAlchemyUnitOfWork(factory) as uow:
                engine_service = InvestigationTaskService(
                    tasks=uow.investigation_tasks,
                    decisions=uow.analyst_decisions,
                    sessions=RepositoryTaskSessionPort(uow.agent_sessions),
                    limits=TaskLimits(),
                )
                assigned = await engine_service.assign(
                    task_id=task.id,
                    actor=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
                    target=TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1"),
                    expected_status_version=1,
                    idempotency_key="assign-orphan",
                    now=NOW,
                )
                await uow.commit()
            assert assigned.task.status is TaskStatus.ASSIGNED
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)

            # The crash: the provisioning/ready row was committed, the lease
            # never was, and the created_at is older than the sweep grace.
            orphan = replace(
                _session(
                    investigation.id,
                    task.id,
                    handoff_id,
                    idempotency_key="crashed-open",
                    state=AgentSessionState.READY,
                ),
                created_at=NOW - timedelta(hours=2),
                opened_at=NOW - timedelta(hours=2),
                updated_at=NOW - timedelta(hours=2),
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.agent_sessions.add(orphan)
                await uow.commit()

            class _Source:
                async def list_orphan_sessions(
                    self, *, grace_seconds: int, limit: int
                ) -> tuple[AgentSessionV1, ...]:
                    async with factory() as db_session:
                        return await SqlAlchemyAgentSessionRepository(
                            db_session
                        ).list_orphan_sessions(grace_seconds=grace_seconds, limit=limit)

                async def list_pending_host_release_sessions(
                    self, *, grace_seconds: int, limit: int
                ) -> tuple[AgentSessionV1, ...]:
                    async with factory() as db_session:
                        return await SqlAlchemyAgentSessionRepository(
                            db_session
                        ).list_pending_host_release_sessions(
                            grace_seconds=grace_seconds, limit=limit
                        )

            host = _ClosingHost()
            releases = HostSessionReleaseService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                host=cast(HostSessionsPort, host),
                wait_seconds=0.0,
            )
            recovered, pending = await recover_orphan_agent_sessions(
                source=_Source(),
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                flush_releases=releases.flush,
                grace_seconds=60,
                batch_size=10,
            )
            assert recovered == 1
            assert orphan.id in host.closed, "the host teardown is requested, never a relaunch"
            assert pending == 0, "the confirmed release is no longer pending"
            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.agent_sessions.get(orphan.id)
                assert stored is not None
                assert stored.state is AgentSessionState.FAILED
                assert stored.close_reason == "orphaned_no_lease"
                events = await uow.investigation_tasks.list_events(task.id)
            assert any(event.idempotency_key == f"host-release:{orphan.id}" for event in events), (
                "the confirmed host release is a durable journal marker"
            )

            # The one-active-session index no longer blocks the task: a human
            # opens a fresh session with a new key.
            operator = _BlockingOperator()
            operator.release.set()
            service = SessionService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                deriver=HmacSessionSecretDeriver("0123456789abcdef0123456789abcdef"),
                operator=operator,
                secret_delivery=_SecretDelivery(),
                disclosure=_allow_disclosure,
                task_limits=TaskLimits(),
                clock=lambda: NOW,
            )
            reopened = await service.open_session(
                investigation.id,
                task_id=task.id,
                adapter_kind=OperatorAdapterKind.CODEX_CLI,
                handoff_id=handoff_id,
                executable="/usr/local/bin/codex",
                actor=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
                idempotency_key="open-after-orphan",
                now=NOW,
            )
            assert reopened.session.state is AgentSessionState.RUNNING
            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored_task = await uow.investigation_tasks.get(task.id)
                assert stored_task is not None
                assert stored_task.status is TaskStatus.RUNNING
                assert stored_task.lease_session_id == reopened.session.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pending_host_release_repairs_a_lost_session(
    temporary_postgres_url: str,
) -> None:
    """R4-F3: a ``lost`` session has ``closed_at`` NULL but must still be released.

    The D7 domain and the 0023 CHECK set ``closed_at`` exactly in
    ``closed|failed``; a ``lost`` session is terminal without it.  After a
    backend crash between the session commit and the host flush, the sweep must
    re-request teardown for that host reference too, or the CLI and its
    provider credential can outlive their lease with no durable release.
    """
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-lost-release")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            old = NOW - timedelta(minutes=10)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                db_now = await uow.investigation_tasks.database_now()
                lost = replace(
                    _session(
                        investigation.id,
                        task.id,
                        handoff_id,
                        idempotency_key="lost-release",
                    ),
                    state=AgentSessionState.LOST,
                    close_reason="lease_expired",
                    opened_at=old,
                    created_at=old,
                    updated_at=old,
                )
                closed = replace(
                    _session(
                        investigation.id,
                        task.id,
                        handoff_id,
                        idempotency_key="closed-release",
                    ),
                    state=AgentSessionState.CLOSED,
                    closed_at=old,
                    opened_at=old,
                    created_at=old,
                    updated_at=old,
                )
                fresh = replace(
                    _session(
                        investigation.id,
                        task.id,
                        handoff_id,
                        idempotency_key="fresh-lost-release",
                    ),
                    state=AgentSessionState.LOST,
                    close_reason="lease_expired",
                    opened_at=db_now,
                    created_at=db_now,
                    updated_at=db_now,
                )
                await uow.agent_sessions.add(lost)
                await uow.agent_sessions.add(closed)
                await uow.agent_sessions.add(fresh)
                await uow.commit()

            class _Source:
                async def list_orphan_sessions(
                    self, *, grace_seconds: int, limit: int
                ) -> tuple[AgentSessionV1, ...]:
                    async with factory() as db_session:
                        return await SqlAlchemyAgentSessionRepository(
                            db_session
                        ).list_orphan_sessions(grace_seconds=grace_seconds, limit=limit)

                async def list_pending_host_release_sessions(
                    self, *, grace_seconds: int, limit: int
                ) -> tuple[AgentSessionV1, ...]:
                    async with factory() as db_session:
                        return await SqlAlchemyAgentSessionRepository(
                            db_session
                        ).list_pending_host_release_sessions(
                            grace_seconds=grace_seconds, limit=limit
                        )

            host = _ClosingHost()
            releases = HostSessionReleaseService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                host=cast(HostSessionsPort, host),
                wait_seconds=0.0,
            )
            recovered, pending = await recover_orphan_agent_sessions(
                source=_Source(),
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                flush_releases=releases.flush,
                grace_seconds=60,
                batch_size=10,
            )
            assert recovered == 0
            assert pending == 2, "the lost and the closed session both await teardown"
            assert set(host.closed) == {lost.id, closed.id}
            async with SqlAlchemyUnitOfWork(factory) as uow:
                events = await uow.investigation_tasks.list_events(task.id)
                stored_fresh = await uow.agent_sessions.get(fresh.id)
            markers = {event.idempotency_key for event in events}
            assert f"host-release:{lost.id}" in markers
            assert f"host-release:{closed.id}" in markers
            assert f"host-release:{fresh.id}" not in markers
            assert stored_fresh is not None
            assert stored_fresh.state is AgentSessionState.LOST

            # The durable marker makes the next pass a clean no-op.
            _, again = await recover_orphan_agent_sessions(
                source=_Source(),
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                flush_releases=releases.flush,
                grace_seconds=60,
                batch_size=10,
            )
            assert again == 0
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_instruction_delivery_repository_cas_and_pending_listing(
    temporary_postgres_url: str,
) -> None:
    """The delivery CAS is durable: token, JSONB diffs and pending drain order."""
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-delivery")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            session_row = _session(
                investigation.id, task.id, handoff_id, idempotency_key="delivery-session"
            )
            first_text = "Check the delivery gate."
            second_text = "Then read the note."
            first = AgentSessionInstructionV1(
                investigation_id=investigation.id,
                task_id=task.id,
                session_id=session_row.id,
                idempotency_key="delivery-1",
                text=first_text,
                text_sha256=instruction_text_sha256(first_text),
                state=InstructionState.QUEUED,
                created_by_kind="human",
                created_by_id="analyst:test",
                queued_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
            second = AgentSessionInstructionV1(
                investigation_id=investigation.id,
                task_id=task.id,
                session_id=session_row.id,
                idempotency_key="delivery-2",
                text=second_text,
                text_sha256=instruction_text_sha256(second_text),
                state=InstructionState.QUEUED,
                created_by_kind="human",
                created_by_id="analyst:test",
                queued_at=NOW + timedelta(seconds=1),
                created_at=NOW,
                updated_at=NOW,
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.agent_sessions.add(session_row)
                await uow.agent_sessions.add_instruction(first)
                await uow.agent_sessions.add_instruction(second)
                await uow.commit()

            token = uuid4()
            delivery_sha = "c" * 64
            async with SqlAlchemyUnitOfWork(factory) as uow:
                pending = await uow.agent_sessions.list_pending_instructions(limit=10)
                assert [item.id for item in pending] == [first.id, second.id]
                reserved = first.with_delivery_reserved(
                    token=token, delivery_sha256=delivery_sha, now=NOW
                )
                stored = await uow.agent_sessions.update_instruction_delivery(
                    reserved,
                    expected_state=InstructionState.QUEUED,
                    expected_delivery_token=None,
                )
                await uow.commit()
            assert stored.delivery_attempts == 1

            async with SqlAlchemyUnitOfWork(factory) as uow:
                reloaded = await uow.agent_sessions.get_instruction_by_token(
                    first.id, token, state=InstructionState.DELIVERING
                )
                assert reloaded is not None
                assert reloaded.delivery_sha256 == delivery_sha
                delivered = reserved.with_delivery_confirmed(
                    delivery_mode=InstructionDeliveryMode.NEXT_RUN,
                    delivery_sha256=delivery_sha,
                    delivery_note_diffs=({"path": "notes/humaines/piste.md", "diff": "+new line"},),
                    now=NOW,
                )
                with pytest.raises(VersionConflictError):
                    await uow.agent_sessions.update_instruction_delivery(
                        delivered,
                        expected_state=InstructionState.DELIVERING,
                        expected_delivery_token=uuid4(),
                    )
                await uow.agent_sessions.update_instruction_delivery(
                    delivered,
                    expected_state=InstructionState.DELIVERING,
                    expected_delivery_token=token,
                )
                await uow.commit()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                proven = await uow.agent_sessions.get_instruction(first.id)
                assert proven is not None
                assert proven.state is InstructionState.DELIVERED
                assert proven.delivery_mode is InstructionDeliveryMode.NEXT_RUN
                assert proven.delivery_sha256 == delivery_sha
                assert proven.delivery_token == token
                assert proven.delivery_note_diffs == (
                    {"path": "notes/humaines/piste.md", "diff": "+new line"},
                )
                assert proven.delivered_at == NOW

            async with SqlAlchemyUnitOfWork(factory) as uow:
                cancelled = await uow.agent_sessions.cancel_instruction(
                    second.id,
                    expected_state=InstructionState.QUEUED,
                    expected_delivery_token=None,
                    reason="handed_off:handoff-1",
                    now=NOW,
                )
                await uow.commit()
            assert cancelled.state is InstructionState.CANCELLED

            async with SqlAlchemyUnitOfWork(factory) as uow:
                assert await uow.agent_sessions.list_pending_instructions(limit=10) == ()
                stored_second = await uow.agent_sessions.get_instruction(second.id)
                assert stored_second is not None
                assert stored_second.cancel_reason == "handed_off:handoff-1"
                assert stored_second.cancelled_at == NOW

            with pytest.raises(VersionConflictError):
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    await uow.agent_sessions.cancel_instruction(
                        second.id,
                        expected_state=InstructionState.QUEUED,
                        expected_delivery_token=None,
                        reason="again",
                        now=NOW,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_pending_instruction_listing_ignores_terminal_session_rows(
    temporary_postgres_url: str,
) -> None:
    """R4-F2: dead-session rows never fill the drain batch and starve a live one.

    The design's candidate is a *session active* carrying a pending
    instruction.  Rows left behind by closed sessions (queued or ambiguous)
    used to occupy the whole ``limit`` regardless of their session, so 50 dead
    rows older than a fresh instruction starved it forever.
    """
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            task = await _seed_task(factory, investigation.id, task_key="lead-starvation")
            handoff_id = await _seed_handoff(factory, investigation.id, task.id)
            old = NOW - timedelta(days=1)
            dead_rows: list[AgentSessionInstructionV1] = []
            async with SqlAlchemyUnitOfWork(factory) as uow:
                for index in range(51):
                    dead_session = replace(
                        _session(
                            investigation.id,
                            task.id,
                            handoff_id,
                            idempotency_key=f"dead-session-{index}",
                        ),
                        state=AgentSessionState.CLOSED,
                        closed_at=old,
                        opened_at=old,
                        created_at=old,
                        updated_at=old,
                    )
                    await uow.agent_sessions.add(dead_session)
                    text = f"dead instruction {index}"
                    queued = AgentSessionInstructionV1(
                        investigation_id=investigation.id,
                        task_id=task.id,
                        session_id=dead_session.id,
                        idempotency_key=f"dead-instruction-{index}",
                        text=text,
                        text_sha256=instruction_text_sha256(text),
                        state=InstructionState.QUEUED,
                        created_by_kind="human",
                        created_by_id="analyst:test",
                        queued_at=old + timedelta(seconds=index),
                        created_at=old,
                        updated_at=old,
                    )
                    if index % 2 == 0:
                        instruction = queued
                    else:
                        # Half of the dead rows are ambiguous attempts: they
                        # are equally unresolvable and equally starving.
                        instruction = queued.with_delivery_reserved(
                            token=uuid4(),
                            delivery_sha256=instruction_text_sha256(text),
                            now=old,
                        ).with_state(
                            InstructionState.DELIVERY_UNKNOWN,
                            now=old,
                            last_delivery_error="host_transport_error",
                        )
                    dead_rows.append(instruction)
                    await uow.agent_sessions.add_instruction(instruction)
                live_session = _session(
                    investigation.id, task.id, handoff_id, idempotency_key="live-session"
                )
                await uow.agent_sessions.add(live_session)
                live_text = "the only live instruction"
                live = AgentSessionInstructionV1(
                    investigation_id=investigation.id,
                    task_id=task.id,
                    session_id=live_session.id,
                    idempotency_key="live-instruction",
                    text=live_text,
                    text_sha256=instruction_text_sha256(live_text),
                    state=InstructionState.QUEUED,
                    created_by_kind="human",
                    created_by_id="analyst:test",
                    queued_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                )
                await uow.agent_sessions.add_instruction(live)
                await uow.commit()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                # The 51 dead rows all pre-date the live one, so the unfiltered
                # (queued_at, id) limit used to return none of the live row.
                bounded = await uow.agent_sessions.list_pending_instructions(limit=50)
                assert [item.id for item in bounded] == [live.id]
                # The rows really exist; only the active-session filter hides them.
                for dead in dead_rows:
                    assert await uow.agent_sessions.get_instruction(dead.id) is not None
        finally:
            await engine.dispose()

    asyncio.run(run())


# ------------------------------------------------ instruction actions route

SESSION_KEY = "0123456789abcdef0123456789abcdef"


async def _seed_route_session(
    session_factory: async_sessionmaker[AsyncSession], investigation_id: UUID
) -> tuple[AgentSessionV1, bytes]:
    """One running task and an active session with a live lease and D7 secret."""
    session_id = uuid4()
    lease_id = uuid4()
    nonce = b"r" * 32
    secret = HmacSessionSecretDeriver(SESSION_KEY).derive(session_id=session_id, nonce=nonce)
    task = InvestigationTaskV1(
        id=uuid4(),
        investigation_id=investigation_id,
        task_key=f"lead-route-{uuid4().hex[:10]}",
        title="Inspect the loader",
        question="Which network gate does the loader use?",
        objective="Identify the first-stage C2 gate.",
        priority=TaskPriority.HIGH,
        status=TaskStatus.RUNNING,
        status_version=2,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=HUMAN_ACTOR,
        created_at=NOW,
        updated_at=NOW,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id="codex:operator-1",
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.investigation_tasks.add(task)
        await uow.commit()
    capabilities = _capabilities()
    session = AgentSessionV1(
        id=session_id,
        investigation_id=investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.AWAITING_INSTRUCTION,
        state_version=2,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=nonce,
        auth_secret_hash=hashlib.sha256(secret).hexdigest(),
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"session-{session_id}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
        handoff_id=None,
        task_lease_id=lease_id,
        task_lease_fence=1,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.agent_sessions.add(session)
        await uow.commit()
    # The D7 verifier and the F2-C revalidation both read a live lease.
    async with session_factory() as db:
        await db.execute(
            sa.text(
                "UPDATE investigation_tasks SET lease_id = :lease_id, "
                "lease_session_id = :session_id, lease_fence = 1, "
                "heartbeat_at = now(), lease_expires_at = now() + interval '10 minutes' "
                "WHERE id = :task_id"
            ),
            {"lease_id": lease_id, "session_id": session_id, "task_id": task.id},
        )
        await db.commit()
    return session, secret


def test_collect_host_io_does_not_hold_postgres_uow_or_task_lock(
    temporary_postgres_url: str,
) -> None:
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session, _secret = await _seed_route_session(factory, investigation.id)
            tracked_uows = _HostIoTrackingUowFactory(factory)
            operator = _BlockingCollectOperator(lambda: tracked_uows.active)
            service = SessionService(
                uow_factory=tracked_uows,
                deriver=HmacSessionSecretDeriver(SESSION_KEY),
                operator=operator,
                disclosure=_allow_disclosure,
                task_limits=TaskLimits(),
                clock=lambda: NOW,
            )
            operator_actor = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1")
            collecting = asyncio.create_task(
                service.collect(
                    investigation.id,
                    session.id,
                    actor=operator_actor,
                    cursor="postgres-collect-1",
                    now=NOW,
                )
            )
            await asyncio.wait_for(operator.entered.wait(), timeout=10)

            assert operator.uow_depth_at_call == 0
            assert tracked_uows.active == 0
            async with SqlAlchemyUnitOfWork(factory) as uow:
                pending = await uow.agent_sessions.get(session.id)
                assert pending is not None
                assert pending.state is AgentSessionState.RECONCILING

            async def heartbeat_while_host_waits() -> None:
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    task_service = InvestigationTaskService(
                        tasks=uow.investigation_tasks,
                        decisions=uow.analyst_decisions,
                        sessions=RepositoryTaskSessionPort(uow.agent_sessions),
                        limits=TaskLimits(),
                    )
                    await task_service.heartbeat(
                        task_id=session.task_id,
                        actor=operator_actor,
                        expected_status_version=2,
                        lease_fence=1,
                        idempotency_key="heartbeat-during-host-collect",
                        now=NOW,
                    )
                    await uow.commit()

            # The operation keeps the host blocked while another transaction
            # takes the investigation/task locks and renews the same fence.
            await asyncio.wait_for(heartbeat_while_host_waits(), timeout=10)
            assert not operator.release.is_set()

            operator.release.set()
            result = await asyncio.wait_for(collecting, timeout=10)
            assert result.session_id == session.id
            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.agent_sessions.get(session.id)
                assert stored is not None
                assert stored.state is AgentSessionState.AWAITING_INSTRUCTION
                assert stored.checkpoint_sha256 == "c" * 64
                task = await uow.investigation_tasks.get(session.task_id)
                assert task is not None and task.lease_fence == 1
        finally:
            if "operator" in locals():
                operator.release.set()
            await engine.dispose()

    asyncio.run(run())


def test_collect_result_is_rejected_after_postgres_lease_revocation(
    temporary_postgres_url: str,
) -> None:
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session, _secret = await _seed_route_session(factory, investigation.id)
            tracked_uows = _HostIoTrackingUowFactory(factory)
            operator = _BlockingCollectOperator(lambda: tracked_uows.active)
            service = SessionService(
                uow_factory=tracked_uows,
                deriver=HmacSessionSecretDeriver(SESSION_KEY),
                operator=operator,
                disclosure=_allow_disclosure,
                task_limits=TaskLimits(),
                clock=lambda: NOW,
            )
            operator_actor = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1")
            collecting = asyncio.create_task(
                service.collect(
                    investigation.id,
                    session.id,
                    actor=operator_actor,
                    cursor="postgres-collect-revoked",
                    now=NOW,
                )
            )
            await asyncio.wait_for(operator.entered.wait(), timeout=10)

            async with SqlAlchemyUnitOfWork(factory) as uow:
                task = await uow.investigation_tasks.get(session.task_id)
                assert task is not None
                task_service = InvestigationTaskService(
                    tasks=uow.investigation_tasks,
                    decisions=uow.analyst_decisions,
                    sessions=RepositoryTaskSessionPort(uow.agent_sessions),
                    limits=TaskLimits(),
                )
                await task_service.cancel(
                    task_id=task.id,
                    actor=HUMAN_ACTOR,
                    expected_status_version=task.status_version,
                    idempotency_key="cancel-during-host-collect",
                    reason="revoke while host result is pending",
                    now=NOW,
                )
                await uow.commit()

            operator.release.set()
            with pytest.raises(SessionStateConflictError):
                await asyncio.wait_for(collecting, timeout=10)

            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.agent_sessions.get(session.id)
                task = await uow.investigation_tasks.get(session.task_id)
                assert stored is not None and stored.state is AgentSessionState.CLOSED
                assert stored.checkpoint_ref is None
                assert task is not None
                assert task.lease_id is None and task.lease_session_id is None
                assert task.lease_fence == 2
            assert operator.collected == [session.id]
        finally:
            if "operator" in locals():
                operator.release.set()
            await engine.dispose()

    asyncio.run(run())


def test_two_postgres_sessions_can_wait_for_host_collection_concurrently(
    temporary_postgres_url: str,
) -> None:
    command.upgrade(_alembic_config(temporary_postgres_url), "head")

    async def run() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session_a, _secret_a = await _seed_route_session(factory, investigation.id)
            session_b, _secret_b = await _seed_route_session(factory, investigation.id)
            tracked_uows = _HostIoTrackingUowFactory(factory)
            operator = _BlockingCollectOperator(lambda: tracked_uows.active)
            service = SessionService(
                uow_factory=tracked_uows,
                deriver=HmacSessionSecretDeriver(SESSION_KEY),
                operator=operator,
                disclosure=_allow_disclosure,
                task_limits=TaskLimits(),
                clock=lambda: NOW,
            )
            operator_actor = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1")
            first = asyncio.create_task(
                service.collect(
                    investigation.id,
                    session_a.id,
                    actor=operator_actor,
                    cursor="two-session-a",
                    now=NOW,
                )
            )
            second = asyncio.create_task(
                service.collect(
                    investigation.id,
                    session_b.id,
                    actor=operator_actor,
                    cursor="two-session-b",
                    now=NOW,
                )
            )
            await asyncio.wait_for(operator.both_entered.wait(), timeout=10)

            assert tracked_uows.active == 0
            assert operator.uow_depth_at_call == 0
            async with SqlAlchemyUnitOfWork(factory) as uow:
                pending_a = await uow.agent_sessions.get(session_a.id)
                pending_b = await uow.agent_sessions.get(session_b.id)
                assert pending_a is not None and pending_a.state is AgentSessionState.RECONCILING
                assert pending_b is not None and pending_b.state is AgentSessionState.RECONCILING

            operator.release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
            assert {result.session_id for result in results} == {session_a.id, session_b.id}
            assert set(operator.collected) == {session_a.id, session_b.id}
        finally:
            if "operator" in locals():
                operator.release.set()
            await engine.dispose()

    asyncio.run(run())


async def _seed_instruction(
    session_factory: async_sessionmaker[AsyncSession],
    session: AgentSessionV1,
    *,
    text: str,
    key: str,
    queued_at: datetime = NOW,
) -> AgentSessionInstructionV1:
    instruction = AgentSessionInstructionV1(
        investigation_id=session.investigation_id,
        task_id=session.task_id,
        session_id=session.id,
        idempotency_key=key,
        text=text,
        text_sha256=instruction_text_sha256(text),
        state=InstructionState.QUEUED,
        created_by_kind="human",
        created_by_id="analyst:test",
        queued_at=queued_at,
        created_at=queued_at,
        updated_at=queued_at,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored, created = await uow.agent_sessions.add_instruction(instruction)
        assert created
        await uow.commit()
    return stored


async def _seed_delivered_instruction(
    session_factory: async_sessionmaker[AsyncSession],
    session: AgentSessionV1,
    *,
    text: str,
    key: str,
) -> AgentSessionInstructionV1:
    instruction = AgentSessionInstructionV1(
        investigation_id=session.investigation_id,
        task_id=session.task_id,
        session_id=session.id,
        idempotency_key=key,
        text=text,
        text_sha256=instruction_text_sha256(text),
        state=InstructionState.DELIVERED,
        created_by_kind="human",
        created_by_id="analyst:test",
        queued_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        delivered_at=NOW,
        delivery_attempts=1,
        delivery_token=uuid4(),
        delivery_mode=InstructionDeliveryMode.NEXT_RUN,
        delivery_sha256=instruction_text_sha256(text),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored, created = await uow.agent_sessions.add_instruction(instruction)
        assert created
        await uow.commit()
    return stored


def _signed_route_request(
    secret: bytes, *, path: str, session_id: UUID, payload: dict[str, object]
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    timestamp_text = str(int(datetime.now(UTC).timestamp()))
    message = build_agent_session_message(
        method="POST",
        path_and_query=path,
        session_id=session_id,
        body=body,
        idempotency_key=None,
        timestamp=timestamp_text,
    )
    signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return body, {
        "content-type": "application/json",
        "x-autowork-agent-session": str(session_id),
        "x-autowork-agent-timestamp": timestamp_text,
        "x-autowork-agent-signature": f"v1={signature}",
    }


def test_instruction_actions_route_on_postgres(migrated_postgres_url: str) -> None:
    """The real route over PostgreSQL: human cancel, signed pull and strict ack."""

    async def run() -> None:
        engine = create_postgres_engine(migrated_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session, secret = await _seed_route_session(factory, investigation.id)
            suffix = uuid4().hex[:8]
            cancel_me = await _seed_instruction(
                factory, session, text="Cancel me.", key=f"pg-route-cancel-{suffix}"
            )
            ack_me = await _seed_instruction(
                factory,
                session,
                text="Ack me.",
                key=f"pg-route-ack-{suffix}",
                queued_at=NOW + timedelta(seconds=1),
            )
            delivered = await _seed_delivered_instruction(
                factory, session, text="Delivered once.", key=f"pg-route-delivered-{suffix}"
            )

            application = FastAPI()
            application.include_router(agent_sessions_router)
            application.state.uow_factory = lambda: SqlAlchemyUnitOfWork(factory)
            application.state.settings = Settings(
                _env_file=None,
                m24_agent_session_key=SESSION_KEY,
                agent_sessions_allow_local_identity=True,
            )
            application.state.identity_provider = LocalIdentityProvider("analyst:test")
            path = (
                f"/api/investigations/{investigation.id}/agent-sessions/{session.id}/instructions"
            )

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                cancelled = await client.post(
                    path,
                    json={
                        "action": "cancel",
                        "instruction_id": str(cancel_me.id),
                        "reason": "obsolete",
                    },
                )
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["state"] == "cancelled"
                assert cancelled.json()["cancel_reason"] == "obsolete"

                body, headers = _signed_route_request(
                    secret, path=path, session_id=session.id, payload={"action": "pull"}
                )
                pulled = await client.post(path, headers=headers, content=body)
                assert pulled.status_code == 200, pulled.text
                assert [item["id"] for item in pulled.json()] == [str(delivered.id)]
                assert pulled.json()[0]["delivery_text"] == "Delivered once."
                assert pulled.json()[0]["delivery_sha256"] == delivered.delivery_sha256
                assert pulled.json()[0]["delivery_text_verified"] is True

                body, headers = _signed_route_request(
                    secret,
                    path=path,
                    session_id=session.id,
                    payload={"action": "ack", "instruction_ids": [str(ack_me.id)]},
                )
                refused = await client.post(path, headers=headers, content=body)
                assert refused.status_code == 409, refused.text
                assert refused.json()["detail"]["code"] == "instruction_not_delivered"

                body, headers = _signed_route_request(
                    secret,
                    path=path,
                    session_id=session.id,
                    payload={"action": "ack", "instruction_ids": [str(delivered.id)]},
                )
                acked = await client.post(path, headers=headers, content=body)
                assert acked.status_code == 200, acked.text
                assert acked.json()[0]["state"] == "acknowledged"
                assert acked.json()[0]["acknowledged_at"] is not None

                body, headers = _signed_route_request(
                    secret, path=path, session_id=session.id, payload={"action": "pull"}
                )
                empty = await client.post(path, headers=headers, content=body)
                assert empty.status_code == 200, empty.text
                assert empty.json() == []

            async with factory() as db:
                rows = (
                    await db.execute(
                        sa.text(
                            "SELECT id, state, cancel_reason FROM "
                            "agent_session_instructions WHERE session_id = :session_id"
                        ),
                        {"session_id": session.id},
                    )
                ).all()
            states = {row.id: row.state for row in rows}
            assert states[cancel_me.id] == "cancelled"
            assert states[ack_me.id] == "queued"
            assert states[delivered.id] == "acknowledged"
        finally:
            await engine.dispose()

    asyncio.run(run())

"""PostgreSQL oracles for the M24.1 instruction delivery drain (lot ID-2).

Disposable migrated database only.  Proves the real compare-and-swap
reservation under two concurrent drainers, the transaction-scoped advisory lock
the lifespan tick uses, and that migration 0027's delivery columns/CHECKs/FKs
accept the phase transitions the service writes (and reject an invalid
combination).
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.agent_session_disclosure import SessionDisclosurePreRead
from cti_app.application.agentic_host_sessions import (
    HostBudgetUsageV1,
    HostInstructionResultV1,
    HostSessionEventsPageV1,
    HostSessionStatusV1,
)
from cti_app.application.agentic_sessions import NoteInstructionResolution
from cti_app.application.instruction_delivery import (
    INSTRUCTION_DRAIN_LOCK_KEY,
    InstructionDeliveryService,
)
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.domain.agentic_operator import (
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionV1,
    InstructionDeliveryMode,
    InstructionState,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorConfinementV1,
    OperatorProbeCommand,
    instruction_text_sha256,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
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
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
GRACE_SECONDS = 120
EXECUTABLE = "/usr/local/bin/codex"
HUMAN_ACTOR = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")


class _AllowDisclosure:
    """Schema/race oracles are not about the disclosure policy."""

    async def pre_read(self, investigation_id: UUID) -> SessionDisclosurePreRead:
        return SessionDisclosurePreRead(
            investigation_id=investigation_id,
            subject_id=uuid4(),
            snapshot=SimpleNamespace(tlp="clear", external_llm_allowed=True),  # type: ignore[arg-type]
        )

    async def require(self, pre_read: SessionDisclosurePreRead, uow: Any) -> None:
        return None


class _FakeNotes:
    async def prepare(
        self, *, investigation_id: UUID, text: str, note_refs: Any
    ) -> NoteInstructionResolution:
        return NoteInstructionResolution(queued_text=text, delivery_text=text)

    async def refresh(self, *, investigation_id: UUID, text: str) -> NoteInstructionResolution:
        return NoteInstructionResolution(queued_text=text, delivery_text=text)


class _FakeHost:
    def __init__(self) -> None:
        self.send_calls: list[dict[str, Any]] = []
        self.status_calls = 0

    async def get_session_status(self, session_id: UUID) -> HostSessionStatusV1:
        self.status_calls += 1
        return HostSessionStatusV1(
            state="awaiting_instruction",
            state_version=1,
            adapter_kind="codex_cli",
            budgets_used=HostBudgetUsageV1(wall_seconds=0, output_bytes=0, instructions=0),
            last_activity_at=NOW,
            log_cursor=0,
        )

    async def send_instruction(
        self, session_id: UUID, request: Any, *, idempotency_key: str
    ) -> HostInstructionResultV1:
        self.send_calls.append(
            {"session_id": session_id, "request": request, "idempotency_key": idempotency_key}
        )
        return HostInstructionResultV1(
            accepted=True, delivered="queued", cursor=len(self.send_calls)
        )

    async def get_events(
        self,
        session_id: UUID,
        *,
        after: int = 0,
        generation: int | None = None,
        limit: int = 500,
    ) -> HostSessionEventsPageV1:
        return HostSessionEventsPageV1(
            generation=1,
            events=(),
            next_cursor=after,
            oldest_retained=0,
            resync_required=False,
        )


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Instruction delivery fixture {suffix}",
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
            title="Instruction delivery fixture",
            slug=f"instruction-delivery-fixture-{suffix}",
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


def _capabilities() -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable=EXECUTABLE,
        version="codex-cli 1.2.3",
        modes=("exec", "resume"),
        supports_resume=True,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        supports_next_run_instruction=True,
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
                argv=(EXECUTABLE, "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "resume", "workspace-write"),
            ),
        ),
    )


async def _seed_leased_session(
    session_factory: async_sessionmaker[AsyncSession], investigation_id: UUID
) -> AgentSessionV1:
    """One running task, one idle backend session and the matching D3 lease."""
    session_id = uuid4()
    lease_id = uuid4()
    task = InvestigationTaskV1(
        id=uuid4(),
        investigation_id=investigation_id,
        task_key=f"lead-{uuid4().hex[:10]}",
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
        auth_secret_nonce=b"s" * 32,
        auth_secret_hash=hashlib.sha256(b"s" * 32).hexdigest(),
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
    # The task-side lease names the session row; the FK requires that order.
    async with session_factory() as db:
        await db.execute(
            sa.text(
                "UPDATE investigation_tasks SET lease_id = :lease_id, "
                "lease_session_id = :session_id, lease_fence = 1, "
                "heartbeat_at = :heartbeat, lease_expires_at = :expires "
                "WHERE id = :task_id"
            ),
            {
                "lease_id": lease_id,
                "session_id": session_id,
                "heartbeat": NOW,
                "expires": NOW + timedelta(minutes=5),
                "task_id": task.id,
            },
        )
        await db.commit()
    return session


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


def _delivery_service(
    session_factory: async_sessionmaker[AsyncSession], host: _FakeHost
) -> InstructionDeliveryService:
    return InstructionDeliveryService(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
        host=host,
        notes=_FakeNotes(),
        disclosure=_AllowDisclosure(),
        clock=lambda: NOW,
        grace_seconds=GRACE_SECONDS,
    )


async def _sqlstate_for(statement: sa.Executable, params: dict[str, object], url: str) -> str:
    engine = create_postgres_engine(url)
    try:
        async with engine.connect() as connection:
            try:
                await connection.execute(statement, params)
                await connection.commit()
            except DBAPIError as exc:
                return str(getattr(exc.orig, "sqlstate", ""))
    finally:
        await engine.dispose()
    return ""


def test_two_concurrent_drainers_reserve_once_and_call_host_once(
    migrated_postgres_url: str,
) -> None:
    async def run() -> None:
        engine = create_postgres_engine(migrated_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session = await _seed_leased_session(factory, investigation.id)
            first = await _seed_instruction(factory, session, text="First.", key="pg-i-1")
            second = await _seed_instruction(
                factory,
                session,
                text="Second.",
                key="pg-i-2",
                queued_at=NOW + timedelta(seconds=1),
            )
            host = _FakeHost()
            drainer_a = _delivery_service(factory, host)
            drainer_b = _delivery_service(factory, host)

            reports = await asyncio.gather(drainer_a.drain_once(), drainer_b.drain_once())
            assert sum(report.delivered for report in reports) == 1
            # One reservation means one host call, even with two drainers.
            assert len(host.send_calls) == 1
            assert host.send_calls[0]["request"].instruction_id == first.id

            async with factory() as db:
                rows = (
                    await db.execute(
                        sa.text(
                            "SELECT id, state, delivery_attempts, delivery_token, "
                            "delivery_sha256 FROM agent_session_instructions "
                            "WHERE session_id = :session_id ORDER BY queued_at, id"
                        ),
                        {"session_id": session.id},
                    )
                ).all()
            assert [row.state for row in rows] == ["delivered", "queued"]
            assert rows[0].delivery_attempts == 1
            assert rows[0].delivery_token is not None
            assert rows[0].delivery_sha256 == instruction_text_sha256("First.")

            # The real CAS refuses a stale expectation.
            async with SqlAlchemyUnitOfWork(factory) as uow:
                delivered = await uow.agent_sessions.get_instruction(first.id)
                assert delivered is not None
                with pytest.raises(VersionConflictError):
                    await uow.agent_sessions.update_instruction_delivery(
                        delivered,
                        expected_state=InstructionState.QUEUED,
                        expected_delivery_token=None,
                    )

            # The next pass delivers the second instruction exactly once.
            later = await drainer_a.drain_once()
            assert later.delivered == 1
            assert len(host.send_calls) == 2
            assert host.send_calls[1]["request"].instruction_id == second.id
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_instruction_drain_advisory_lock_is_transaction_scoped(
    migrated_postgres_url: str,
) -> None:
    statement = sa.text(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(CAST(:lock_key AS text), 0))"
    )
    params = {"lock_key": INSTRUCTION_DRAIN_LOCK_KEY}

    async def run() -> None:
        engine = create_postgres_engine(migrated_postgres_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as holder:
                async with holder.begin():
                    assert bool(await holder.scalar(statement, params)) is True
                    async with factory() as contender:
                        async with contender.begin():
                            assert bool(await contender.scalar(statement, params)) is False
                # The scope ends with the holder's transaction.
                async with factory() as after:
                    async with after.begin():
                        assert bool(await after.scalar(statement, params)) is True
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_0027_accepts_delivery_transitions_and_rejects_invalid_combos(
    migrated_postgres_url: str,
) -> None:
    async def run() -> None:
        engine = create_postgres_engine(migrated_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session = await _seed_leased_session(factory, investigation.id)
            instruction = await _seed_instruction(
                factory, session, text="Schema transition.", key="pg-i-3"
            )
            token = uuid4()
            delivery_sha = instruction_text_sha256("Schema transition.")

            async with SqlAlchemyUnitOfWork(factory) as uow:
                current = await uow.agent_sessions.get_instruction(instruction.id)
                assert current is not None
                reserved = current.with_delivery_reserved(
                    token=token, delivery_sha256=delivery_sha, now=NOW
                )
                await uow.agent_sessions.update_instruction_delivery(
                    reserved,
                    expected_state=InstructionState.QUEUED,
                    expected_delivery_token=None,
                )
                await uow.commit()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                current = await uow.agent_sessions.get_instruction(instruction.id)
                assert current is not None
                delivered = current.with_state(
                    InstructionState.DELIVERED,
                    now=NOW + timedelta(seconds=1),
                    delivery_mode=InstructionDeliveryMode.NEXT_RUN,
                    delivery_sha256=delivery_sha,
                    delivery_note_diffs=(
                        {
                            "path": "notes/humaines/piste.md",
                            "current_revision_id": "rev-" + "a" * 32,
                        },
                    ),
                )
                await uow.agent_sessions.update_instruction_delivery(
                    delivered,
                    expected_state=InstructionState.DELIVERING,
                    expected_delivery_token=token,
                )
                await uow.commit()

            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.agent_sessions.get_instruction(instruction.id)
                assert stored is not None
                assert stored.state is InstructionState.DELIVERED
                assert stored.delivery_mode is InstructionDeliveryMode.NEXT_RUN
                assert stored.delivery_sha256 == delivery_sha
                assert stored.delivery_note_diffs is not None
                assert stored.delivered_at is not None

            # An ambiguous attempt is a valid 0027 row too.
            second = await _seed_instruction(
                factory,
                session,
                text="Ambiguous.",
                key="pg-i-4",
                queued_at=NOW + timedelta(seconds=1),
            )
            second_token = uuid4()
            async with SqlAlchemyUnitOfWork(factory) as uow:
                current = await uow.agent_sessions.get_instruction(second.id)
                assert current is not None
                reserved = current.with_delivery_reserved(
                    token=second_token,
                    delivery_sha256=instruction_text_sha256("Ambiguous."),
                    now=NOW,
                )
                await uow.agent_sessions.update_instruction_delivery(
                    reserved,
                    expected_state=InstructionState.QUEUED,
                    expected_delivery_token=None,
                )
                await uow.commit()
            async with SqlAlchemyUnitOfWork(factory) as uow:
                current = await uow.agent_sessions.get_instruction(second.id)
                assert current is not None
                unknown = current.with_state(
                    InstructionState.DELIVERY_UNKNOWN,
                    now=NOW + timedelta(seconds=1),
                    last_delivery_error="host_transport_error",
                )
                await uow.agent_sessions.update_instruction_delivery(
                    unknown,
                    expected_state=InstructionState.DELIVERING,
                    expected_delivery_token=second_token,
                )
                await uow.commit()

            # A queued row can never carry a proven-delivery mode (0027 CHECK).
            assert (
                await _sqlstate_for(
                    sa.text(
                        "UPDATE agent_session_instructions SET delivery_mode = 'next_run' "
                        "WHERE id = :id"
                    ),
                    {"id": second.id},
                    migrated_postgres_url,
                )
                == "23514"
            )
            # The composite (session, investigation, task) FK is enforced.
            assert (
                await _sqlstate_for(
                    sa.text(
                        "INSERT INTO agent_session_instructions (id, investigation_id, "
                        "task_id, session_id, idempotency_key, body, body_sha256, state, "
                        "created_by_actor_kind, created_by_actor_id, queued_at, "
                        "delivery_attempts, created_at, updated_at) VALUES "
                        "(:id, :investigation_id, :task_id, :session_id, :key, 'x', "
                        ":body_sha256, 'queued', 'human', 'analyst:test', :now, 0, "
                        ":now, :now)"
                    ),
                    {
                        "id": uuid4(),
                        "investigation_id": uuid4(),
                        "task_id": session.task_id,
                        "session_id": session.id,
                        "key": f"pg-fk-{uuid4().hex}",
                        "body_sha256": hashlib.sha256(b"x").hexdigest(),
                        "now": NOW,
                    },
                    migrated_postgres_url,
                )
                == "23503"
            )
        finally:
            await engine.dispose()

    asyncio.run(run())

"""PostgreSQL oracles for the M24.1 instruction delivery drain (lot ID-2).

Disposable migrated database only.  Proves the real compare-and-swap
reservation under two concurrent drainers, the session-level advisory lock the
lifespan tick uses (mutual exclusion without an idle transaction), and that
migration 0027's delivery columns/CHECKs/FKs accept the phase transitions the
service writes (and reject an invalid combination).
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.api.agent_sessions import router as agent_sessions_router
from cti_app.api.main import _instruction_drain_tick_lock
from cti_app.application.agent_session_disclosure import SessionDisclosurePreRead
from cti_app.application.agent_session_verifier import (
    HmacSessionSecretDeriver,
    session_secret_hash,
)
from cti_app.application.agentic_host_sessions import (
    HostBudgetUsageV1,
    HostInstructionResultV1,
    HostReasonRequestV1,
    HostSessionEventsPageV1,
    HostSessionStatusV1,
)
from cti_app.application.agentic_sessions import (
    NoteInstructionResolution,
    SessionSecret,
    SessionService,
)
from cti_app.application.instruction_delivery import InstructionDeliveryService
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.config import Settings
from cti_app.domain.agentic_operator import (
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionV1,
    InstructionDeliveryMode,
    InstructionState,
    InterruptSessionRequest,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorConfinementV1,
    OperatorProbeCommand,
    PrepareSessionRequest,
    ProbeCapabilitiesRequest,
    StartSessionRequest,
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
from cti_app.infrastructure.agentic.host_operator import host_idempotency_key
from cti_app.infrastructure.agentic.wiring import build_agentic_operator_ports
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sidecars.workspace_agent.auth import TOKEN_FILE_NAME  # noqa: E402
from sidecars.workspace_agent.sessions import supervisor as sessions_supervisor  # noqa: E402
from sidecars.workspace_agent.sessions.supervisor import (  # noqa: E402
    FakeProfile,
    ProfileRegistry,
)

from tests.agentic_support import FixedContextProvider, context  # noqa: E402
from tests.workspace_agent.session_shim_support import (  # noqa: E402
    FakeBackend,
    ShimHarness,
    shim_environment,
)
from tests.workspace_agent.sessions_support import (  # noqa: E402
    fake_script,
    read_observations,
    running_session_agent,
    write_fake_script,
)

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
    # The lease is stamped by the database clock, and delivery fences it on
    # that clock (R4-F4), so seed it there rather than with the fixed test NOW.
    async with session_factory() as db:
        await db.execute(
            sa.text(
                "UPDATE investigation_tasks SET lease_id = :lease_id, "
                "lease_session_id = :session_id, lease_fence = 1, "
                "heartbeat_at = statement_timestamp(), "
                "lease_expires_at = statement_timestamp() + interval '5 minutes' "
                "WHERE id = :task_id"
            ),
            {
                "lease_id": lease_id,
                "session_id": session_id,
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


def test_terminal_session_stale_delivering_is_reconciled_and_cancelable(
    migrated_postgres_url: str,
) -> None:
    """R4B-F1: a stale ``delivering`` attempt of a lost session still resolves.

    The crash window between phases A and C is documented; when the task lease
    then expires and the sweep marks the session ``lost``, the row used to be
    invisible to the active-session drain list and stayed ``delivering`` for
    ever, refusing human cancellation and every transfer preflight.  The
    dedicated reconciliation query covers ALL sessions and the CAS keeps it
    safe.
    """

    async def run() -> None:
        engine = create_postgres_engine(migrated_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            session = await _seed_leased_session(factory, investigation.id)
            attempted_at = NOW - timedelta(seconds=GRACE_SECONDS + 60)
            text = "Delivered before the crash?"
            queued = await _seed_instruction(
                factory,
                session,
                text=text,
                key=f"pg-stale-delivering-{uuid4().hex[:8]}",
                queued_at=attempted_at,
            )
            token = uuid4()
            reserved = queued.with_delivery_reserved(
                token=token,
                delivery_sha256=instruction_text_sha256(text),
                now=attempted_at,
            )
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.agent_sessions.update_instruction_delivery(
                    reserved,
                    expected_state=InstructionState.QUEUED,
                    expected_delivery_token=None,
                )
                await uow.commit()

            # The CLI can no longer renew its lease: the sweep marks it lost.
            async with SqlAlchemyUnitOfWork(factory) as uow:
                current = await uow.agent_sessions.get(session.id)
                assert current is not None
                lost = current.with_state(AgentSessionState.LOST, now=NOW)
                stored = await uow.agent_sessions.update_state(
                    lost, expected_state_version=current.state_version
                )
                assert stored is not None
                await uow.commit()

            host = _FakeHost()
            report = await _delivery_service(factory, host).drain_once()

            assert report.stale_unknown == 1
            assert host.send_calls == []
            async with SqlAlchemyUnitOfWork(factory) as uow:
                recovered = await uow.agent_sessions.get_instruction(queued.id)
            assert recovered is not None
            assert recovered.state is InstructionState.DELIVERY_UNKNOWN
            assert recovered.delivery_token == token
            assert recovered.last_delivery_error == "delivery_timeout"

            # A human may now cancel the ambiguous instruction (and a transfer
            # preflight, which refuses any ``delivering`` row, no longer does).
            cancel_service = SessionService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                deriver=None,
                clock=lambda: NOW,
            )
            cancelled = await cancel_service.cancel_instruction(
                investigation.id,
                session.id,
                instruction_id=queued.id,
                reason="the session is lost",
                actor=HUMAN_ACTOR,
                now=NOW,
            )
            assert cancelled.state is InstructionState.CANCELLED
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_instruction_drain_lock_excludes_without_an_idle_transaction(
    migrated_postgres_url: str,
) -> None:
    """F3: the drain tick excludes other drainers but holds no transaction.

    The lock is session-level on one dedicated connection and released
    explicitly: while the holder performs the batch's host I/O, its PostgreSQL
    session must be ``idle``, not ``idle in transaction``.
    """

    async def run() -> None:
        engine = create_postgres_engine(migrated_postgres_url)
        try:
            factory = create_session_factory(engine)
            async with _instruction_drain_tick_lock(factory) as granted:
                assert granted is True
                # A second drainer is excluded while the first holds the tick.
                async with _instruction_drain_tick_lock(factory) as contender:
                    assert contender is False
                async with engine.connect() as observer:
                    holder_state = await observer.scalar(
                        sa.text(
                            "SELECT activity.state FROM pg_locks AS locks "
                            "JOIN pg_stat_activity AS activity "
                            "ON activity.pid = locks.pid "
                            "WHERE locks.locktype = 'advisory' "
                            "AND locks.granted AND locks.objsubid = 1"
                        )
                    )
                assert holder_state == "idle"
            # The explicit unlock ends with the tick.
            async with _instruction_drain_tick_lock(factory) as after:
                assert after is True
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


# ---------------------------------------------------------------------------
# WIRE-1 end-to-end: real sidecar receive point -> real drain -> real shim ack
# ---------------------------------------------------------------------------

RECEIVE_POINT_TEXT = "Reprends la verification du chargeur synthetique."
RECEIVE_POINT_KEY = "pg-wire1-receive-point"
SESSION_KEY = "k" * 32
SESSION_NONCE = b"r" * 32


class _CachedFakeCodexProfile(FakeProfile):  # type: ignore[misc]
    """A real adapter kind backed by the deterministic fake CLI.

    Probe facts are cached like the production profiles cache theirs: the
    probe recorded before prepare and the one the secret hand-off re-reads
    must carry the same ``probe_sha256``.
    """

    adapter_kind = "codex_cli"
    requires_manifest = True

    def __init__(self) -> None:
        super().__init__(supports_resume=True, simulate_network_attestation=True)
        self._cached_probe: Any = None

    def probe(self, *, now: str) -> Any:
        if self._cached_probe is None:
            self._cached_probe = super().probe(now=now)
        return self._cached_probe


def _instruction_script(*, long_running: bool) -> dict[str, object]:
    steps: list[dict[str, object]] = [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "write", "path": "notes/finding.md", "text": "# synthetic finding\n"},
        {"op": "write", "path": "result.md", "text": "synthetic result\n"},
    ]
    if long_running:
        # Keep the next run (and its relay) alive while the real shim pulls and
        # acknowledges; the test interrupts it explicitly.
        steps.append({"op": "sleep", "seconds": 10.0})
    steps.append({"op": "exit", "code": 0})
    return fake_script(steps)


async def _wait_host_state(
    host: Any, session_id: UUID, state: str, *, timeout_seconds: float = 20.0
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last: Any = None
    while time.monotonic() < deadline:
        last = await host.get_session_status(session_id)
        if last.state == state:
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"host session did not reach {state}: {last!r}")


async def _wait_until_async(predicate: Any, *, timeout_seconds: float = 15.0) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met before the deadline")


async def _seed_receive_point_session(
    session_factory: async_sessionmaker[AsyncSession],
    investigation_id: UUID,
    *,
    session_id: UUID,
    capabilities: OperatorCapabilitiesV1,
    auth_nonce: bytes,
    auth_secret_hash: str,
) -> UUID:
    """One running task, one imported lease and the awaiting session row."""

    task_id = uuid4()
    lease_id = uuid4()
    task = InvestigationTaskV1(
        id=task_id,
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
    session = AgentSessionV1(
        id=session_id,
        investigation_id=investigation_id,
        task_id=task_id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.AWAITING_INSTRUCTION,
        state_version=2,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=auth_nonce,
        auth_secret_hash=auth_secret_hash,
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"session-{session_id}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=str(session_id),
        handoff_id=None,
        task_lease_id=lease_id,
        task_lease_fence=1,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.agent_sessions.add(session)
        await uow.commit()
    # The signed ack route validates the lease against the real clock, while
    # the drain uses the fixed test clock: one expiry must satisfy both.
    lease_expires_at = max(datetime.now(UTC), NOW) + timedelta(minutes=30)
    async with session_factory() as db:
        await db.execute(
            sa.text(
                "UPDATE investigation_tasks SET lease_id = :lease_id, "
                "lease_session_id = :session_id, lease_fence = 1, heartbeat_at = :heartbeat, "
                "lease_expires_at = :expires WHERE id = :task_id"
            ),
            {
                "lease_id": lease_id,
                "session_id": session_id,
                "heartbeat": NOW,
                "expires": lease_expires_at,
                "task_id": task_id,
            },
        )
        await db.commit()
    return task_id


def test_receive_point_drains_the_next_run_and_acknowledges_via_the_real_shim(
    migrated_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WIRE-1: real sidecar + real drain + real shim, one continuous chain.

    A real workspace agent runs the fake CLI profile.  A clean run end is the
    host receive point; the backend enqueues through ``SessionService`` and the
    real ``InstructionDeliveryService`` drains it against the real host client.
    The host then proves ``instruction_delivered`` (id + SHA) and the fake CLI
    records the exact block (id + SHA).  The real shim pulls and acknowledges
    through the real relay to a loopback recorder; the recorder's exact signed
    request is replayed into the real FastAPI route, which marks the row
    ``acknowledged`` in PostgreSQL.  Only the HTTP hop between the relay and
    the in-process app is replayed, because the app is not served on a socket.
    """

    base = Path(tempfile.mkdtemp(prefix="m24recv-", dir="/tmp"))
    try:
        monkeypatch.setattr(
            sessions_supervisor,
            "default_profiles",
            lambda: ProfileRegistry((_CachedFakeCodexProfile(),)),
        )
        with FakeBackend() as backend:
            monkeypatch.setenv("AGENT_SESSIONS_BROKER_BACKEND_URL", backend.url)
            with running_session_agent(base) as running, running.client() as client:
                workspace_id = running.ensure_workspace(client)
                asyncio.run(
                    _receive_point_flow(migrated_postgres_url, running, backend, workspace_id)
                )
    finally:
        shutil.rmtree(base, ignore_errors=True)


async def _receive_point_flow(
    migrated_postgres_url: str,
    running: Any,
    backend: FakeBackend,
    workspace_id: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        factory = create_session_factory(engine)
        settings = Settings(
            _env_file=None,
            m24_agent_session_key=SESSION_KEY,
            workspace_agent_socket=str(running.socket_path),
            workspace_agent_token_file=str(running.runtime_dir / TOKEN_FILE_NAME),
        )
        ports = build_agentic_operator_ports(
            settings,
            context_provider=FixedContextProvider(context(workspace_id=workspace_id)),
            clock=lambda: NOW,
        )
        operator = ports.operator
        delivery = ports.secret_delivery
        host = ports.host_sessions
        assert operator is not None and delivery is not None and host is not None
        try:
            capabilities = await operator.probe_capabilities(
                ProbeCapabilitiesRequest(
                    adapter_kind=OperatorAdapterKind.CODEX_CLI, executable=EXECUTABLE
                )
            )
            # WIRE-1: the real host advertises the next-run capability.
            assert capabilities.supports_next_run_instruction is True
            investigation = await _seed_investigation(factory)
            session_id = uuid4()
            task_id = uuid4()
            prepared = await operator.prepare_session(
                PrepareSessionRequest(
                    session_id=session_id,
                    investigation_id=investigation.id,
                    task_id=task_id,
                    adapter_kind=OperatorAdapterKind.CODEX_CLI,
                    handoff_id=uuid4(),
                    capability_sha256=capabilities.capability_sha256,
                )
            )
            secret = HmacSessionSecretDeriver(SESSION_KEY).derive(
                session_id=session_id, nonce=SESSION_NONCE
            )
            await delivery.deliver_session_secret(
                session_id=session_id,
                investigation_id=investigation.id,
                task_id=task_id,
                adapter_kind=OperatorAdapterKind.CODEX_CLI,
                host_session_ref=prepared.host_session_ref,
                secret=SessionSecret.from_bytes(secret),
                idempotency_key="pg-wire1-secret",
            )
            # Production order: the closed manifest follows the secret hand-off
            # and precedes any run (the profile requires one).
            await operator.set_tool_manifest(
                session_id,
                host_session_ref=prepared.host_session_ref,
                capability_sha256=capabilities.capability_sha256,
            )
            write_fake_script(
                running, _instruction_script(long_running=False), session_id=str(session_id)
            )
            started = await operator.start_session(
                StartSessionRequest(
                    session_id=session_id,
                    adapter_kind=OperatorAdapterKind.CODEX_CLI,
                    host_session_ref=prepared.host_session_ref,
                    capability_sha256=capabilities.capability_sha256,
                )
            )
            assert started.alive is True
            await _wait_host_state(host, session_id, "awaiting_instruction")

            await _seed_receive_point_session(
                factory,
                investigation.id,
                session_id=session_id,
                capabilities=capabilities,
                auth_nonce=SESSION_NONCE,
                auth_secret_hash=session_secret_hash(secret),
            )

            async def _allow_disclosure(_investigation_id: UUID) -> None:
                return None

            service = SessionService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                deriver=None,
                operator=None,
                disclosure=_allow_disclosure,
                clock=lambda: NOW,
            )
            enqueued = await service.enqueue_instruction(
                investigation.id,
                session_id,
                text=RECEIVE_POINT_TEXT,
                actor=HUMAN_ACTOR,
                idempotency_key=RECEIVE_POINT_KEY,
            )
            assert enqueued.state is InstructionState.QUEUED

            # The next run lingers so the real shim can reach the relay.
            write_fake_script(
                running, _instruction_script(long_running=True), session_id=str(session_id)
            )
            drainer = InstructionDeliveryService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(factory),
                host=host,
                notes=None,
                disclosure=_AllowDisclosure(),
                clock=lambda: NOW,
                grace_seconds=GRACE_SECONDS,
            )
            report = await drainer.drain_once()
            assert report.delivered == 1, report

            async with SqlAlchemyUnitOfWork(factory) as uow:
                stored = await uow.agent_sessions.get_instruction(enqueued.id)
                session_row = await uow.agent_sessions.get(session_id)
            assert stored is not None and session_row is not None
            assert stored.state is InstructionState.DELIVERED
            assert stored.delivery_mode is InstructionDeliveryMode.NEXT_RUN
            assert stored.delivery_sha256 == instruction_text_sha256(RECEIVE_POINT_TEXT)
            assert session_row.state is AgentSessionState.RUNNING
            await _wait_host_state(host, session_id, "running")

            events = await host.get_events(session_id, after=0, limit=200)
            delivered_events = [
                event for event in events.events if event.kind == "instruction_delivered"
            ]
            assert len(delivered_events) == 1, delivered_events
            assert delivered_events[0].payload == {
                "instruction_id": str(stored.id),
                "sha256": stored.delivery_sha256,
                "mode": "next_run",
                "run": 2,
            }

            seen = await _wait_until_async(
                lambda: read_observations(running, str(session_id)).get("instruction")
            )
            assert isinstance(seen, dict)
            assert seen["instruction_id"] == str(stored.id)
            assert seen["sha256"] == stored.delivery_sha256
            assert seen["text_sha256"] == stored.delivery_sha256

            # The real shim signs the two fixed operator actions and sends them
            # through the real relay; the recorder keeps the exact wire bytes.
            broker_socket = running.session_dir(str(session_id)) / "ipc" / "broker.sock"
            await _wait_until_async(lambda: broker_socket.is_socket())
            harness = ShimHarness(secret=secret, session_id=str(session_id))
            environment = shim_environment(
                base=harness.base,
                socket_path=harness.socket_path,
                backend_url="http://127.0.0.1:1",
                session_id=str(session_id),
            )
            del environment["AUTOWORK_SHIM_TEST_BACKEND_URL"]
            environment["AUTOWORK_BROKER_SOCKET"] = str(broker_socket)
            harness.environment = environment
            with harness as shim:
                shim.initialize()
                shim.call_tool(
                    "autowork_instruction_pending",
                    {"investigation_id": str(investigation.id)},
                )
                shim.call_tool(
                    "autowork_instruction_ack",
                    {
                        "investigation_id": str(investigation.id),
                        "instruction_ids": [str(stored.id)],
                    },
                )
            recorded = backend.requests
            assert len(recorded) == 2, recorded
            assert recorded[0].json_body() == {"action": "pull"}
            assert recorded[1].json_body() == {
                "action": "ack",
                "instruction_ids": [str(stored.id)],
            }

            # Replay the shim's exact signed requests into the real route.  The
            # app is bound to the same PostgreSQL unit of work and the same
            # session key the shim signed with.
            app = FastAPI()
            app.include_router(agent_sessions_router)
            app.state.uow_factory = lambda: SqlAlchemyUnitOfWork(factory)
            app.state.settings = settings

            async def replay(record: Any) -> httpx.Response:
                headers = {
                    name: value
                    for name, value in record.headers.items()
                    if name.lower() != "content-length"
                }
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as api:
                    return await api.request(
                        record.method, record.path, content=record.body, headers=headers
                    )

            pulled = await replay(recorded[0])
            assert pulled.status_code == 200, pulled.text
            pulled_rows = pulled.json()
            assert [row["id"] for row in pulled_rows] == [str(stored.id)]
            assert pulled_rows[0]["delivery_text"] == RECEIVE_POINT_TEXT
            assert pulled_rows[0]["state"] == "delivered"

            acknowledged = await replay(recorded[1])
            assert acknowledged.status_code == 200, acknowledged.text
            assert acknowledged.json()[0]["state"] == "acknowledged"
            async with SqlAlchemyUnitOfWork(factory) as uow:
                final = await uow.agent_sessions.get_instruction(stored.id)
            assert final is not None and final.state is InstructionState.ACKNOWLEDGED

            interrupted = await operator.interrupt_session(
                InterruptSessionRequest(
                    session_id=session_id,
                    host_session_ref=prepared.host_session_ref,
                    reason="wire-1 receive point complete",
                )
            )
            assert interrupted.stopped is True
            await _wait_host_state(host, session_id, "stopped")
            closed = await host.close_session(
                session_id,
                HostReasonRequestV1(reason="wire-1 receive point complete"),
                idempotency_key=host_idempotency_key(
                    "close", session_id, "wire-1 receive point complete"
                ),
            )
            assert closed.state == "closed"
        finally:
            await ports.aclose()
    finally:
        await engine.dispose()

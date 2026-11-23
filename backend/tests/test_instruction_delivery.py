"""Oracles for the M24.1 instruction delivery drain (F3-D §3.3, lot ID-2).

The harness composes the canonical in-memory repositories, a fake raw host
session client (the design's port, not the adapter), a fake note resolver and
the *real* split disclosure checker, with a controlled clock.  It proves the
backend-side scenarios INS-01..INS-08 and the restart part of INS-10; the host
supervisor scenarios and INS-09 belong to later lots.  PostgreSQL CAS/race
oracles live in ``tests/integration/test_instruction_delivery_postgres.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.agent_session_disclosure import (
    build_session_disclosure_checker,
    build_session_disclosure_gate,
)
from cti_app.application.agentic_host_sessions import (
    HOST_MAX_INSTRUCTION_BYTES,
    HostBudgetUsageV1,
    HostInstructionResultV1,
    HostSessionEventsPageV1,
    HostSessionEventV1,
    HostSessionNotFoundError,
    HostSessionStatusV1,
)
from cti_app.application.agentic_sessions import (
    NoteInstructionResolution,
    SessionIdempotencyConflictError,
    SessionService,
)
from cti_app.application.instruction_delivery import (
    InstructionAttemptOutcome,
    InstructionDeliveryService,
    instruction_delivery_idempotency_key,
)
from cti_app.application.investigation_persistence import (
    InMemoryAgentSessionRepository,
    InMemoryInvestigationTaskRepository,
)
from cti_app.application.workspace_agent import WorkspaceLocalUnavailableError
from cti_app.config import Settings
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
from cti_app.domain.investigation import (
    DisclosureBounds,
    EnvelopeAuthorityKind,
    InvestigationBudget,
    InvestigationEnvelope,
    TLPLevel,
)
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
INVESTIGATION_ID = uuid4()
EXECUTABLE = "/usr/local/bin/codex"
HUMAN_ACTOR = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
GRACE_SECONDS = 120


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _InvestigationRepository:
    def __init__(self) -> None:
        self._investigation = SimpleNamespace(
            id=INVESTIGATION_ID,
            subject_id=uuid4(),
            synthesis_artifact_id=uuid4(),
        )

    async def get(self, investigation_id: UUID) -> Any:
        return self._investigation if investigation_id == self._investigation.id else None


class _EnvelopeRepository:
    def __init__(self, harness: _Harness) -> None:
        self._harness = harness

    async def get(self, investigation_id: UUID) -> Any:
        return self._harness.envelope if investigation_id == INVESTIGATION_ID else None


class _DecisionRepository:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _ArticleSnapshots:
    def __init__(self) -> None:
        self.tlp = "clear"
        self.external_llm_allowed = True

    async def resolve(self, synthesis_artifact_id: UUID) -> Any:
        return SimpleNamespace(tlp=self.tlp, external_llm_allowed=self.external_llm_allowed)


class _FakeUnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.analyst_investigations = harness.investigations
        self.analyst_decisions = harness.decisions
        self.investigation_envelopes = harness.envelopes
        self.investigation_tasks = harness.tasks
        self.agent_sessions = harness.sessions

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _FakeNotes:
    """The canonical note resolver surface: queued text plus a visible diff."""

    def __init__(self) -> None:
        self.delivery_text: str | None = None
        self.diffs: tuple[dict[str, str], ...] = ()
        self.refresh_error: Exception | None = None
        self.prepare_calls = 0
        self.refresh_calls = 0

    async def prepare(
        self, *, investigation_id: UUID, text: str, note_refs: Any
    ) -> NoteInstructionResolution:
        self.prepare_calls += 1
        return NoteInstructionResolution(queued_text=text, delivery_text=text)

    async def refresh(self, *, investigation_id: UUID, text: str) -> NoteInstructionResolution:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        delivered = self.delivery_text if self.delivery_text is not None else text
        return NoteInstructionResolution(
            queued_text=text, delivery_text=delivered, note_diffs=self.diffs
        )


class _FakeHost:
    """The raw ``HostSessionsPort`` subset the drain uses; never launches a CLI."""

    def __init__(self) -> None:
        self.status_state = "awaiting_instruction"
        self.status_error: Exception | None = None
        self.status_calls = 0
        self.send_calls: list[dict[str, Any]] = []
        self.send_error: Exception | None = None
        self.accepted = True
        self.delivered = "queued"
        self.events: list[HostSessionEventV1] = []
        self.events_error: Exception | None = None
        self.resync_required = False
        self.before_send: Any = None
        self.unreachable: set[UUID] = set()

    async def get_session_status(self, session_id: UUID) -> HostSessionStatusV1:
        self.status_calls += 1
        if session_id in self.unreachable:
            raise WorkspaceLocalUnavailableError("the host session is lost")
        if self.status_error is not None:
            raise self.status_error
        return HostSessionStatusV1(
            state=self.status_state,
            state_version=1,
            adapter_kind="codex_cli",
            budgets_used=HostBudgetUsageV1(wall_seconds=0, output_bytes=0, instructions=0),
            last_activity_at=NOW,
            log_cursor=0,
        )

    async def send_instruction(
        self, session_id: UUID, request: Any, *, idempotency_key: str
    ) -> HostInstructionResultV1:
        if self.before_send is not None:
            await self.before_send()
        if self.send_error is not None:
            raise self.send_error
        self.send_calls.append(
            {"session_id": session_id, "request": request, "idempotency_key": idempotency_key}
        )
        return HostInstructionResultV1(
            accepted=self.accepted, delivered=self.delivered, cursor=len(self.send_calls)
        )

    async def get_events(
        self,
        session_id: UUID,
        *,
        after: int = 0,
        generation: int | None = None,
        limit: int = 500,
    ) -> HostSessionEventsPageV1:
        if session_id in self.unreachable:
            raise WorkspaceLocalUnavailableError("the host session is lost")
        if self.events_error is not None:
            raise self.events_error
        events = tuple(event for event in self.events if event.seq >= after)
        next_cursor = events[-1].seq + 1 if events else after
        return HostSessionEventsPageV1(
            generation=1,
            events=events,
            next_cursor=next_cursor,
            oldest_retained=0,
            resync_required=self.resync_required,
        )


def _capabilities(*, next_run: bool = True) -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable=EXECUTABLE,
        version="codex-cli 1.2.3",
        modes=("exec", "resume"),
        supports_resume=True,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        supports_next_run_instruction=next_run,
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


class _Harness:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.investigations = _InvestigationRepository()
        self.decisions = _DecisionRepository()
        self.envelopes = _EnvelopeRepository(self)
        self.tasks = InMemoryInvestigationTaskRepository(clock=self.clock)
        self.sessions = InMemoryAgentSessionRepository()
        self.snapshots = _ArticleSnapshots()
        self.notes = _FakeNotes()
        self.host = _FakeHost()
        self.envelope = InvestigationEnvelope(
            investigation_id=INVESTIGATION_ID,
            allowed_providers=("bridge",),
            budget=InvestigationBudget(limits={}),
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.CLEAR,
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            approval_decision_id=uuid4(),
            approval_actor_id="analyst:test",
            authority_kind=EnvelopeAuthorityKind.HUMAN_G2,
        )
        self.disclosure = build_session_disclosure_checker(
            uow_factory=self.uow_factory,
            article_snapshots=self.snapshots,
            settings=Settings(advisor_disclosure_profile="standard"),
        )
        self.gate = build_session_disclosure_gate(
            uow_factory=self.uow_factory,
            article_snapshots=self.snapshots,
            settings=Settings(advisor_disclosure_profile="standard"),
        )
        self.delivery = InstructionDeliveryService(
            uow_factory=self.uow_factory,
            host=self.host,
            notes=self.notes,
            disclosure=self.disclosure,
            clock=self.clock,
            grace_seconds=GRACE_SECONDS,
        )
        self.service = SessionService(
            uow_factory=self.uow_factory,
            deriver=None,
            note_resolver=self.notes,
            disclosure=self.gate,
            instruction_delivery=self.delivery,
            clock=self.clock,
        )
        self.task: InvestigationTaskV1 | None = None

    def uow_factory(self) -> _FakeUnitOfWork:
        return _FakeUnitOfWork(self)

    async def seed_queued_instruction(
        self,
        session: AgentSessionV1,
        *,
        text: str,
        key: str,
        queued_at: datetime = NOW,
    ) -> AgentSessionInstructionV1:
        instruction = AgentSessionInstructionV1(
            investigation_id=INVESTIGATION_ID,
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
        stored, _created = await self.sessions.add_instruction(instruction)
        return stored

    async def seed_session(
        self,
        *,
        state: AgentSessionState = AgentSessionState.RUNNING,
        capability: bool = True,
        task_status: TaskStatus = TaskStatus.RUNNING,
        lease_expires_in: int = 300,
    ) -> AgentSessionV1:
        session_id = uuid4()
        lease_id = uuid4()
        task = InvestigationTaskV1(
            id=uuid4(),
            investigation_id=INVESTIGATION_ID,
            task_key=f"lead-{uuid4().hex[:8]}",
            title="Inspect the loader",
            question="Which network gate does the loader use?",
            objective="Identify the first-stage C2 gate.",
            priority=TaskPriority.HIGH,
            status=task_status,
            status_version=2,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=HUMAN_ACTOR,
            created_at=NOW,
            updated_at=NOW,
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id="codex:operator-1",
            lease_id=lease_id,
            lease_session_id=session_id,
            lease_fence=1,
            heartbeat_at=NOW,
            lease_expires_at=NOW + timedelta(seconds=lease_expires_in),
        )
        await self.tasks.add(task)
        capabilities = _capabilities(next_run=capability)
        session = AgentSessionV1(
            id=session_id,
            investigation_id=INVESTIGATION_ID,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            state=state,
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
        await self.sessions.add(session)
        self.task = task
        return session

    async def enqueue(
        self,
        session: AgentSessionV1,
        *,
        text: str,
        key: str,
        note_refs: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> Any:
        return await self.service.enqueue_instruction_with_notes(
            INVESTIGATION_ID,
            session.id,
            text=text,
            actor=HUMAN_ACTOR,
            idempotency_key=key,
            note_refs=note_refs,
            now=now,
        )

    async def instruction(self, instruction_id: UUID) -> AgentSessionInstructionV1:
        stored = await self.sessions.get_instruction(instruction_id)
        assert stored is not None
        return stored

    def replace_fence(self) -> None:
        assert self.task is not None
        self.task = replace(self.task, lease_fence=2)
        self.tasks._tasks[self.task.id] = self.task


# --------------------------------------------------------------- INS-01/02


async def test_idle_session_delivers_immediately_on_enqueue() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)

    enqueued = await harness.enqueue(session, text="Check the second-stage gate.", key="i-1")

    assert enqueued.delivered_live is True
    assert enqueued.queue_position == 0
    assert enqueued.instruction.state is InstructionState.DELIVERED
    assert enqueued.instruction.delivery_mode is InstructionDeliveryMode.NEXT_RUN
    assert enqueued.instruction.delivery_attempts == 1
    assert enqueued.instruction.delivery_sha256 == instruction_text_sha256(
        "Check the second-stage gate."
    )
    assert len(harness.host.send_calls) == 1
    request = harness.host.send_calls[0]["request"]
    assert request.text == "Check the second-stage gate."
    assert request.sha256 == enqueued.instruction.delivery_sha256
    stored_session = await harness.sessions.get(session.id)
    assert stored_session is not None
    assert stored_session.state is AgentSessionState.RUNNING


async def test_running_session_defers_then_delivers_once_at_receive_point() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    harness.host.status_state = "running"

    enqueued = await harness.enqueue(session, text="Inspect the dropper next.", key="i-2")
    assert enqueued.delivered_live is False
    assert enqueued.instruction.state is InstructionState.QUEUED
    assert harness.host.send_calls == []

    deferred = await harness.delivery.drain_once()
    assert deferred.deferred == 1
    assert harness.host.send_calls == []

    # The host finishes its run and reaches the technical receive point.
    harness.host.status_state = "awaiting_instruction"
    delivered = await harness.delivery.drain_once()
    assert delivered.delivered == 1
    assert len(harness.host.send_calls) == 1
    stored = await harness.instruction(enqueued.instruction.id)
    assert stored.state is InstructionState.DELIVERED
    assert stored.delivery_mode is InstructionDeliveryMode.NEXT_RUN
    session_row = await harness.sessions.get(session.id)
    assert session_row is not None
    assert session_row.state is AgentSessionState.RUNNING

    again = await harness.delivery.drain_once()
    assert again.scanned == 0
    assert len(harness.host.send_calls) == 1


# ------------------------------------------------------------------- INS-03


async def test_two_instructions_deliver_in_order_with_their_own_ids() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    first = await harness.enqueue(session, text="First: dump the config.", key="i-3a", now=NOW)
    second = await harness.enqueue(
        session,
        text="Second: trace the socket.",
        key="i-3b",
        now=NOW + timedelta(seconds=1),
    )
    assert first.instruction.state is InstructionState.QUEUED
    assert second.instruction.state is InstructionState.QUEUED

    harness.host.status_state = "awaiting_instruction"
    pass_one = await harness.delivery.drain_once()
    assert pass_one.delivered == 1
    assert len(harness.host.send_calls) == 1
    assert harness.host.send_calls[0]["request"].instruction_id == first.instruction.id
    assert (await harness.instruction(second.instruction.id)).state is InstructionState.QUEUED

    pass_two = await harness.delivery.drain_once()
    assert pass_two.delivered == 1
    assert len(harness.host.send_calls) == 2
    assert harness.host.send_calls[1]["request"].instruction_id == second.instruction.id
    assert harness.host.send_calls[1]["request"].text == "Second: trace the socket."


async def test_delivery_round_robin_serves_sparse_session_within_two_passes() -> None:
    """A deep queue in one session cannot occupy every bounded drain pass."""
    harness = _Harness()
    first_session = await harness.seed_session(state=AgentSessionState.RUNNING)
    second_session = await harness.seed_session(state=AgentSessionState.RUNNING)
    # Put the busy queue first under the old global (queued_at, id) scan and
    # first in the new deterministic UUID rotation as well.
    busy, sparse = sorted((first_session, second_session), key=lambda item: item.id.int)
    busy_instructions = [
        await harness.seed_queued_instruction(
            busy,
            text=f"Busy instruction {index}",
            key=f"i-busy-{index}",
            queued_at=NOW - timedelta(seconds=10 - index),
        )
        for index in range(5)
    ]
    sparse_instruction = await harness.seed_queued_instruction(
        sparse, text="Sparse instruction", key="i-sparse", queued_at=NOW
    )
    delivery = InstructionDeliveryService(
        uow_factory=harness.uow_factory,
        host=harness.host,
        notes=harness.notes,
        disclosure=harness.disclosure,
        clock=harness.clock,
        grace_seconds=GRACE_SECONDS,
        batch_size=1,
    )

    first_pass = await delivery.drain_once(now=NOW)
    second_pass = await delivery.drain_once(now=NOW)

    assert first_pass.scanned == second_pass.scanned == 1
    assert first_pass.delivered == second_pass.delivered == 1
    assert [call["session_id"] for call in harness.host.send_calls] == [busy.id, sparse.id]
    assert [call["request"].instruction_id for call in harness.host.send_calls] == [
        busy_instructions[0].id,
        sparse_instruction.id,
    ]
    assert (await harness.instruction(sparse_instruction.id)).state is InstructionState.DELIVERED

    replay = await delivery.deliver_instruction(sparse_instruction.id, now=NOW)
    assert replay.outcome is InstructionAttemptOutcome.DEFERRED
    assert len(harness.host.send_calls) == 2

    third_pass = await delivery.drain_once(now=NOW)
    assert third_pass.delivered == 1
    assert harness.host.send_calls[2]["session_id"] == busy.id
    assert harness.host.send_calls[2]["request"].instruction_id == busy_instructions[1].id


async def test_delivery_below_batch_limit_still_selects_one_oldest_per_session() -> None:
    harness = _Harness()
    first_session = await harness.seed_session(state=AgentSessionState.RUNNING)
    second_session = await harness.seed_session(state=AgentSessionState.RUNNING)
    busy, sparse = sorted((first_session, second_session), key=lambda item: item.id.int)
    oldest = await harness.seed_queued_instruction(
        busy, text="Oldest", key="i-under-limit-oldest", queued_at=NOW
    )
    later = await harness.seed_queued_instruction(
        busy,
        text="Later",
        key="i-under-limit-later",
        queued_at=NOW + timedelta(seconds=1),
    )
    other = await harness.seed_queued_instruction(
        sparse, text="Other session", key="i-under-limit-other", queued_at=NOW
    )
    delivery = InstructionDeliveryService(
        uow_factory=harness.uow_factory,
        host=harness.host,
        notes=harness.notes,
        disclosure=harness.disclosure,
        clock=harness.clock,
        grace_seconds=GRACE_SECONDS,
        batch_size=3,
    )

    report = await delivery.drain_once(now=NOW)

    assert report.scanned == 2
    assert report.delivered == 2
    assert [call["session_id"] for call in harness.host.send_calls] == [busy.id, sparse.id]
    assert [call["request"].instruction_id for call in harness.host.send_calls] == [
        oldest.id,
        other.id,
    ]
    assert (await harness.instruction(later.id)).state is InstructionState.QUEUED


# ------------------------------------------------------------------- INS-04


async def test_replayed_enqueue_and_delivery_never_call_host_twice() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)

    enqueued = await harness.enqueue(session, text="Only once.", key="i-4")
    assert len(harness.host.send_calls) == 1

    replay = await harness.enqueue(session, text="Only once.", key="i-4")
    assert replay.instruction.id == enqueued.instruction.id
    assert len(harness.host.send_calls) == 1

    again = await harness.delivery.deliver_instruction(enqueued.instruction.id)
    assert again.outcome is InstructionAttemptOutcome.DEFERRED
    assert len(harness.host.send_calls) == 1

    await harness.delivery.drain_once()
    assert len(harness.host.send_calls) == 1

    with pytest.raises(SessionIdempotencyConflictError):
        await harness.enqueue(session, text="A different body.", key="i-4")


async def test_host_idempotency_key_binds_session_instruction_and_text() -> None:
    session_id = uuid4()
    key = instruction_delivery_idempotency_key(session_id, "instruction-1", "a" * 64)
    assert key.startswith("m24i-")
    assert key == instruction_delivery_idempotency_key(session_id, "instruction-1", "a" * 64)
    assert key != instruction_delivery_idempotency_key(session_id, "instruction-1", "b" * 64)
    assert key != instruction_delivery_idempotency_key(uuid4(), "instruction-1", "a" * 64)


# ------------------------------------------------------------------- INS-05


async def test_note_changed_after_queue_delivers_current_revision_with_visible_diff() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.enqueue(
        session,
        text="Follow the note.",
        key="i-5",
        note_refs=("notes/humaines/piste.md",),
    )
    diff = {
        "path": "notes/humaines/piste.md",
        "queued_revision_id": "rev-" + "1" * 32,
        "queued_sha256": "1" * 64,
        "current_revision_id": "rev-" + "2" * 32,
        "current_sha256": "2" * 64,
    }
    delivery_text = (
        "Follow the note.\n\n[notes modifiées depuis la mise en file]\n"
        "- notes/humaines/piste.md: rev-1111 -> rev-2222"
    )
    harness.notes.diffs = (diff,)
    harness.notes.delivery_text = delivery_text
    harness.host.status_state = "awaiting_instruction"

    report = await harness.delivery.drain_once()
    assert report.delivered == 1
    sent = harness.host.send_calls[0]["request"]
    assert sent.text == delivery_text
    assert sent.sha256 == instruction_text_sha256(delivery_text)
    stored = await harness.instruction(queued.instruction.id)
    assert stored.state is InstructionState.DELIVERED
    assert stored.delivery_sha256 == instruction_text_sha256(delivery_text)
    assert stored.delivery_note_diffs is not None
    assert dict(stored.delivery_note_diffs[0]) == diff


async def test_note_without_current_revision_refuses_delivery() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.enqueue(
        session,
        text="Follow the note.",
        key="i-5b",
        note_refs=("notes/humaines/piste.md",),
    )
    harness.notes.diffs = (
        {
            "path": "notes/humaines/piste.md",
            "queued_revision_id": "rev-" + "1" * 32,
            "queued_sha256": "1" * 64,
            "current_revision_id": "",
            "current_sha256": "",
        },
    )
    harness.notes.delivery_text = "Follow the note."

    report = await harness.delivery.drain_once()
    assert report.refused == 1
    assert harness.host.send_calls == []
    stored = await harness.instruction(queued.instruction.id)
    assert stored.state is InstructionState.QUEUED
    assert stored.last_delivery_error == "note_ref_unresolved"
    assert stored.delivery_attempts == 0


# ------------------------------------------------------------------- INS-06


async def test_lost_session_keeps_instruction_queued_and_never_delivers() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.enqueue(session, text="Do not deliver this.", key="i-6")
    harness.host.status_state = "lost"

    first = await harness.delivery.drain_once()
    assert first.refused == 1
    assert harness.host.send_calls == []
    stored = await harness.instruction(queued.instruction.id)
    assert stored.state is InstructionState.QUEUED
    assert stored.last_delivery_error == "host_session_lost"

    # The host disappears entirely: still no delivery, still queued.
    harness.host.status_error = HostSessionNotFoundError("session gone")
    second = await harness.delivery.drain_once()
    assert second.refused == 1
    assert harness.host.send_calls == []
    assert (await harness.instruction(queued.instruction.id)).state is InstructionState.QUEUED


# ------------------------------------------------------------------- INS-07


async def test_revoked_disclosure_refuses_delivery_without_consuming() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.enqueue(session, text="This must not leak.", key="i-7")
    harness.envelope = replace(
        harness.envelope,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=False,
        ),
    )
    harness.host.status_state = "awaiting_instruction"

    report = await harness.delivery.drain_once()
    assert report.refused == 1
    assert harness.host.send_calls == []
    stored = await harness.instruction(queued.instruction.id)
    assert stored.state is InstructionState.QUEUED
    assert stored.delivery_attempts == 0
    assert stored.delivery_token is None
    assert stored.last_delivery_error is not None
    assert stored.last_delivery_error.startswith("disclosure_denied:")
    assert "external_llm_not_allowed" in stored.last_delivery_error


# ------------------------------------------------------------------- INS-08


async def test_transport_timeout_is_delivery_unknown_and_reconciled_without_resend() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.enqueue(session, text="Ambiguous attempt.", key="i-8")
    harness.host.status_state = "awaiting_instruction"
    harness.host.send_error = WorkspaceLocalUnavailableError("host timed out")

    report = await harness.delivery.drain_once()
    assert report.unknown == 1
    assert len(harness.host.send_calls) == 0
    stored = await harness.instruction(queued.instruction.id)
    assert stored.state is InstructionState.DELIVERY_UNKNOWN
    assert stored.delivery_token is not None
    assert stored.delivery_sha256 == instruction_text_sha256("Ambiguous attempt.")
    assert stored.delivery_attempts == 1

    # The host journal proves the delivery happened: no re-send.
    harness.host.send_error = None
    assert stored.delivery_sha256 is not None
    harness.host.events = [
        HostSessionEventV1(
            seq=3,
            kind="instruction_delivered",
            at=NOW,
            payload={
                "instruction_id": str(stored.id),
                "sha256": stored.delivery_sha256,
                "mode": "next_run",
            },
        )
    ]
    harness.clock.advance(GRACE_SECONDS + 1)
    reconciled = await harness.delivery.drain_once()
    assert reconciled.reconciled_delivered == 1
    assert harness.host.send_calls == []
    proven = await harness.instruction(queued.instruction.id)
    assert proven.state is InstructionState.DELIVERED
    assert proven.delivery_mode is InstructionDeliveryMode.NEXT_RUN


# ------------------------------------------------------- host byte bound (R4-F1)


async def test_text_at_the_host_capacity_is_delivered_whole() -> None:
    """R4-F1: the delivered SHA covers exactly the text the run received."""
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    text = "x" * HOST_MAX_INSTRUCTION_BYTES

    enqueued = await harness.enqueue(session, text=text, key="i-bound")

    assert enqueued.delivered_live is True
    assert len(harness.host.send_calls) == 1
    request = harness.host.send_calls[0]["request"]
    assert request.text == text
    assert request.sha256 == instruction_text_sha256(text)
    stored = await harness.instruction(enqueued.instruction.id)
    assert stored.state is InstructionState.DELIVERED
    assert stored.delivery_sha256 == instruction_text_sha256(text)


async def test_text_one_byte_over_the_host_capacity_is_refused_visibly() -> None:
    """R4-F1: an unsendable text is never announced delivered or truncated."""
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    text = "x" * (HOST_MAX_INSTRUCTION_BYTES + 1)

    enqueued = await harness.enqueue(session, text=text, key="i-bound-over")

    assert enqueued.delivered_live is False
    assert harness.host.send_calls == []
    stored = await harness.instruction(enqueued.instruction.id)
    assert stored.state is InstructionState.QUEUED
    assert stored.delivered_at is None
    assert stored.delivery_attempts == 0
    assert stored.last_delivery_error == "instruction_text_exceeds_host_bound"

    report = await harness.delivery.drain_once()
    assert report.refused == 1
    assert harness.host.send_calls == []
    assert (await harness.instruction(enqueued.instruction.id)).state is InstructionState.QUEUED


# ------------------------------------------------------- drain candidates (R4-F2)


async def test_dead_session_rows_never_fill_the_batch_and_starve_a_live_one() -> None:
    """R4-F2: the candidate set is active sessions, so a dead row cannot starve."""
    harness = _Harness()
    dead = await harness.seed_session(state=AgentSessionState.LOST)
    await harness.seed_queued_instruction(
        dead, text="orphaned instruction", key="i-dead", queued_at=NOW - timedelta(seconds=600)
    )
    live = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.seed_queued_instruction(
        live, text="live instruction", key="i-live", queued_at=NOW
    )
    harness.host.status_state = "awaiting_instruction"
    service = InstructionDeliveryService(
        uow_factory=harness.uow_factory,
        host=harness.host,
        notes=harness.notes,
        disclosure=harness.disclosure,
        clock=harness.clock,
        grace_seconds=GRACE_SECONDS,
        batch_size=1,
    )

    report = await service.drain_once()

    assert report.scanned == 1
    assert report.delivered == 1
    assert [call["request"].text for call in harness.host.send_calls] == ["live instruction"]
    assert (await harness.instruction(queued.id)).state is InstructionState.DELIVERED


async def _seed_stale_delivering(
    harness: _Harness,
    session: AgentSessionV1,
    *,
    key: str,
    text: str,
    attempted_at: datetime,
) -> tuple[AgentSessionInstructionV1, UUID]:
    """A phase-A reservation whose phase C never ran (crash window)."""
    queued = await harness.seed_queued_instruction(
        session, text=text, key=key, queued_at=attempted_at
    )
    token = uuid4()
    reserved = queued.with_delivery_reserved(
        token=token,
        delivery_sha256=instruction_text_sha256(text),
        now=attempted_at,
    )
    await harness.sessions.update_instruction_delivery(
        reserved, expected_state=InstructionState.QUEUED, expected_delivery_token=None
    )
    return queued, token


async def _seed_delivery_unknown(
    harness: _Harness,
    session: AgentSessionV1,
    *,
    key: str,
    text: str,
    unknown_at: datetime,
) -> AgentSessionInstructionV1:
    """A reserved attempt whose phase C never committed the receipt."""
    queued = await harness.seed_queued_instruction(
        session, text=text, key=key, queued_at=unknown_at
    )
    reserved = queued.with_delivery_reserved(
        token=uuid4(),
        delivery_sha256=instruction_text_sha256(text),
        now=unknown_at,
    )
    ambiguous = reserved.with_state(
        InstructionState.DELIVERY_UNKNOWN,
        now=unknown_at,
        last_delivery_error="host_transport_error",
    )
    await harness.sessions.update_instruction_delivery(
        ambiguous, expected_state=InstructionState.QUEUED, expected_delivery_token=None
    )
    return ambiguous


async def test_terminal_session_stale_delivering_is_reconciled_and_cancelable() -> None:
    """R4B-F1: a crash whose session later became lost must still resolve.

    The active-session filter protects the delivery batch from dead rows, but
    it must never hide a stale ``delivering`` attempt: the design converts it
    to ``delivery_unknown`` after the grace so a human can cancel or hand the
    task off (F3-D §3.5).  Before the dedicated reconciliation query the row
    stayed ``delivering`` forever and both actions were refused.
    """
    harness = _Harness()
    attempted_at = NOW - timedelta(seconds=GRACE_SECONDS + 60)
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    queued, token = await _seed_stale_delivering(
        harness,
        session,
        key="i-lost-stale",
        text="Crash on a lost session.",
        attempted_at=attempted_at,
    )
    # The lease expired and the sweep marked the session lost after the crash.
    stored = await harness.sessions.get(session.id)
    assert stored is not None
    await harness.sessions.update_state(
        stored.with_state(AgentSessionState.LOST, now=NOW),
        expected_state_version=stored.state_version,
    )

    report = await harness.delivery.drain_once()

    assert report.stale_unknown == 1
    recovered = await harness.instruction(queued.id)
    assert recovered.state is InstructionState.DELIVERY_UNKNOWN
    assert recovered.delivery_token == token
    assert recovered.last_delivery_error == "delivery_timeout"

    cancelled = await harness.service.cancel_instruction(
        INVESTIGATION_ID,
        session.id,
        instruction_id=queued.id,
        reason="the session is lost",
        actor=HUMAN_ACTOR,
        now=NOW,
    )
    assert cancelled.state is InstructionState.CANCELLED


async def test_terminal_session_delivery_proof_is_still_extracted() -> None:
    """R4B-F2: an ambiguous attempt of a lost session can still be proven.

    The host journal is the delivery proof (D7).  Once the session is
    terminal, reconciliation stays read-only: a matching
    ``instruction_delivered`` event promotes the row to delivered so the
    handoff cites consumed history instead of cancelling a delivered
    instruction as "never delivered".
    """
    harness = _Harness()
    unknown_at = NOW - timedelta(seconds=GRACE_SECONDS + 60)
    session = await harness.seed_session(state=AgentSessionState.LOST)
    text = "Consumed by the lost run."
    queued = await harness.seed_queued_instruction(
        session, text=text, key="i-lost-unknown", queued_at=unknown_at
    )
    sha = instruction_text_sha256(text)
    reserved = queued.with_delivery_reserved(token=uuid4(), delivery_sha256=sha, now=unknown_at)
    ambiguous = reserved.with_state(
        InstructionState.DELIVERY_UNKNOWN, now=unknown_at, last_delivery_error="delivery_timeout"
    )
    await harness.sessions.update_instruction_delivery(
        ambiguous, expected_state=InstructionState.QUEUED, expected_delivery_token=None
    )
    harness.host.events = [
        HostSessionEventV1(
            seq=1,
            kind="instruction_delivered",
            at=unknown_at,
            payload={"instruction_id": str(queued.id), "sha256": sha, "mode": "next_run"},
        )
    ]

    report = await harness.delivery.drain_once()

    assert report.reconciled_delivered == 1
    assert harness.host.send_calls == []
    proven = await harness.instruction(queued.id)
    assert proven.state is InstructionState.DELIVERED
    assert proven.delivery_mode is InstructionDeliveryMode.NEXT_RUN
    assert proven.delivery_sha256 == sha


async def test_terminal_unknown_rows_never_starve_a_live_reconciliation() -> None:
    """R4C-F1: live and terminal unknown rows get separate reconciliation quotas.

    Fifty ``delivery_unknown`` rows of lost sessions whose hosts are gone can
    never be proven.  A single oldest-first bounded query never reaches the
    newer ambiguous row of a live session behind them, so its journal proof is
    not read and its next ``queued`` instruction stays blocked behind it.
    Reconciliation now reads each population with its own bound, live first.
    """
    harness = _Harness()
    unknown_at = NOW - timedelta(seconds=600)
    for index in range(50):
        lost = await harness.seed_session(state=AgentSessionState.LOST)
        harness.host.unreachable.add(lost.id)
        await _seed_delivery_unknown(
            harness,
            lost,
            key=f"i-lost-unknown-{index}",
            text=f"unprovable instruction {index}",
            unknown_at=unknown_at + timedelta(seconds=index),
        )
    live = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    text = "Consumed by the live run."
    live_unknown = await _seed_delivery_unknown(
        harness,
        live,
        key="i-live-unknown",
        text=text,
        unknown_at=NOW - timedelta(seconds=GRACE_SECONDS + 1),
    )
    harness.host.events = [
        HostSessionEventV1(
            seq=1,
            kind="instruction_delivered",
            at=live_unknown.delivery_unknown_at or live_unknown.updated_at,
            payload={
                "instruction_id": str(live_unknown.id),
                "sha256": live_unknown.delivery_sha256,
                "mode": "next_run",
            },
        )
    ]

    report = await harness.delivery.drain_once()

    assert report.reconciled_delivered == 1
    assert (await harness.instruction(live_unknown.id)).state is InstructionState.DELIVERED


async def test_dead_unknown_rows_of_live_sessions_never_starve_the_batch() -> None:
    """R4-F2 residual: non-deliverable rows cannot fill the delivery batch.

    Fifty older ``delivery_unknown`` rows whose sessions are still
    ``running`` (the sweep has not marked them lost yet) are neither
    deliverable nor resolvable.  They used to fill the bounded batch ahead of
    a live queued instruction; five ticks sent nothing.  Only ``queued`` rows
    are delivery candidates, so the live instruction is reached immediately.
    """
    harness = _Harness()
    unknown_at = NOW - timedelta(seconds=600)
    for index in range(50):
        dead = await harness.seed_session(state=AgentSessionState.RUNNING)
        harness.host.unreachable.add(dead.id)
        await _seed_delivery_unknown(
            harness,
            dead,
            key=f"i-dead-unknown-{index}",
            text=f"dead instruction {index}",
            unknown_at=unknown_at + timedelta(seconds=index),
        )
    live = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    queued = await harness.seed_queued_instruction(
        live, text="live instruction", key="i-live-fair", queued_at=NOW
    )

    report = await harness.delivery.drain_once()

    assert report.delivered == 1
    assert [call["session_id"] for call in harness.host.send_calls] == [live.id]
    assert (await harness.instruction(queued.id)).state is InstructionState.DELIVERED


# ----------------------------------------------------- database clock (R4-F4)


class _DatabaseClockOffset:
    """A task repository whose database clock runs ahead of the process clock."""

    def __init__(self, inner: Any, offset: timedelta) -> None:
        self._inner = inner
        self._offset = offset

    async def database_now(self) -> datetime:
        inner: datetime = await self._inner.database_now()
        return inner + self._offset

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def test_lease_expired_on_the_database_clock_is_refused() -> None:
    """R4-F4: fencing reads the database clock, not the backend process clock."""
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    # The lease is still valid on the (slow) process clock but already expired
    # in PostgreSQL, which stamped it.
    harness.tasks = _DatabaseClockOffset(harness.tasks, timedelta(seconds=600))

    enqueued = await harness.enqueue(session, text="db clock fence", key="i-db-clock")

    assert enqueued.delivered_live is False
    assert harness.host.send_calls == []
    stored = await harness.instruction(enqueued.instruction.id)
    assert stored.state is InstructionState.QUEUED
    assert stored.delivery_attempts == 0
    assert stored.last_delivery_error == "lease_expired"


async def test_lease_expired_on_the_database_clock_between_phases_is_unknown() -> None:
    """R4-F4: phase C re-checks the lease on the database clock too."""
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.seed_queued_instruction(session, text="late fence", key="i-db-clock-c")
    harness.host.status_state = "awaiting_instruction"

    async def skew_database_clock() -> None:
        harness.tasks = _DatabaseClockOffset(harness.tasks, timedelta(seconds=600))

    harness.host.before_send = skew_database_clock

    result = await harness.delivery.deliver_instruction(queued.id)

    assert result.outcome is InstructionAttemptOutcome.UNKNOWN
    stored = await harness.instruction(queued.id)
    assert stored.state is InstructionState.DELIVERY_UNKNOWN
    assert stored.last_delivery_error == "lease_expired"
    assert stored.delivered_at is None


# -------------------------------------------------- reconciliation text (R4-F5)


async def test_journal_reconciliation_rebuilds_the_delivered_text_with_notes() -> None:
    """R4-F5: a journal-proven delivery keeps the exact delivered text and diff."""
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    diff = {
        "path": "notes/humaines/piste.md",
        "queued_revision_id": "rev-1",
        "queued_sha256": "1" * 64,
        "current_revision_id": "rev-2",
        "current_sha256": "2" * 64,
    }
    delivery_text = (
        "Follow the note.\n[notes modifiées depuis la mise en file]\n"
        "- notes/humaines/piste.md: rev-1 -> rev-2"
    )
    harness.notes.diffs = (diff,)
    harness.notes.delivery_text = delivery_text
    queued = await harness.enqueue(
        session,
        text="Follow the note.",
        key="i-reconcile-note",
        note_refs=("notes/humaines/piste.md",),
    )
    assert queued.instruction.state is InstructionState.QUEUED

    harness.host.status_state = "awaiting_instruction"
    harness.host.send_error = WorkspaceLocalUnavailableError("host timed out")
    unknown = await harness.delivery.drain_once()
    assert unknown.unknown == 1
    ambiguous = await harness.instruction(queued.instruction.id)
    assert ambiguous.state is InstructionState.DELIVERY_UNKNOWN
    assert ambiguous.delivery_note_diffs is None
    assert ambiguous.delivery_sha256 == instruction_text_sha256(delivery_text)

    # The host journal proves the delivery of exactly that text.
    harness.host.send_error = None
    harness.host.events = [
        HostSessionEventV1(
            seq=1,
            kind="instruction_delivered",
            at=NOW,
            payload={
                "instruction_id": str(ambiguous.id),
                "sha256": ambiguous.delivery_sha256,
                "mode": "next_run",
            },
        )
    ]
    harness.clock.advance(GRACE_SECONDS + 1)
    reconciled = await harness.delivery.drain_once()
    assert reconciled.reconciled_delivered == 1
    proven = await harness.instruction(queued.instruction.id)
    assert proven.state is InstructionState.DELIVERED
    assert proven.delivery_note_diffs is not None
    assert dict(proven.delivery_note_diffs[0]) == diff

    operator = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1")
    pending = await harness.service.pull_delivered_instructions(
        INVESTIGATION_ID, session.id, actor=operator
    )
    assert len(pending) == 1
    assert pending[0].delivery_text == delivery_text
    assert pending[0].delivery_text_verified is True


async def test_journal_reconciliation_leaves_moved_notes_unverified_not_invented() -> None:
    """R4-F5 limit: when the notes moved again, the pull says unverified.

    The journal proves the host accepted *some* text with the reserved SHA;
    the backend cannot reconstruct it from note revisions that changed since,
    so no diff is invented and the pull exposes ``delivery_text_verified``
    false rather than claiming a text it cannot prove.
    """
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    harness.notes.diffs = (
        {
            "path": "notes/humaines/piste.md",
            "queued_revision_id": "rev-1",
            "queued_sha256": "1" * 64,
            "current_revision_id": "rev-2",
            "current_sha256": "2" * 64,
        },
    )
    harness.notes.delivery_text = (
        "Follow the note.\n[notes modifiées depuis la mise en file]\n"
        "- notes/humaines/piste.md: rev-1 -> rev-2"
    )
    queued = await harness.enqueue(
        session,
        text="Follow the note.",
        key="i-reconcile-moved",
        note_refs=("notes/humaines/piste.md",),
    )
    harness.host.status_state = "awaiting_instruction"
    harness.host.send_error = WorkspaceLocalUnavailableError("host timed out")
    await harness.delivery.drain_once()
    ambiguous = await harness.instruction(queued.instruction.id)
    assert ambiguous.state is InstructionState.DELIVERY_UNKNOWN

    # The note moved again before the journal could be reconciled.
    harness.notes.diffs = (
        {
            "path": "notes/humaines/piste.md",
            "queued_revision_id": "rev-1",
            "queued_sha256": "1" * 64,
            "current_revision_id": "rev-3",
            "current_sha256": "3" * 64,
        },
    )
    harness.notes.delivery_text = (
        "Follow the note.\n[notes modifiées depuis la mise en file]\n"
        "- notes/humaines/piste.md: rev-1 -> rev-3"
    )
    harness.host.send_error = None
    harness.host.events = [
        HostSessionEventV1(
            seq=1,
            kind="instruction_delivered",
            at=NOW,
            payload={
                "instruction_id": str(ambiguous.id),
                "sha256": ambiguous.delivery_sha256,
                "mode": "next_run",
            },
        )
    ]
    harness.clock.advance(GRACE_SECONDS + 1)
    reconciled = await harness.delivery.drain_once()
    assert reconciled.reconciled_delivered == 1
    proven = await harness.instruction(queued.instruction.id)
    assert proven.state is InstructionState.DELIVERED
    assert proven.delivery_note_diffs is None

    operator = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1")
    pending = await harness.service.pull_delivered_instructions(
        INVESTIGATION_ID, session.id, actor=operator
    )
    assert len(pending) == 1
    assert pending[0].delivery_text == "Follow the note."
    assert pending[0].delivery_text_verified is False


# ---------------------------------------------------------- phase C re-checks


async def test_lease_expired_between_phases_a_and_c_is_not_delivered() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.enqueue(session, text="Lease race.", key="i-fence")
    harness.host.status_state = "awaiting_instruction"
    harness.host.before_send = _expire_lease_before_send(harness)

    report = await harness.delivery.drain_once()
    assert report.unknown == 1
    assert len(harness.host.send_calls) == 1
    stored = await harness.instruction(queued.instruction.id)
    assert stored.state is InstructionState.DELIVERY_UNKNOWN
    assert stored.last_delivery_error == "lease_expired"
    assert stored.delivered_at is None


def _expire_lease_before_send(harness: _Harness) -> Any:
    async def expire() -> None:
        assert harness.task is not None
        # A complete lease still validates (expiry follows heartbeat) while
        # being expired relative to the delivery clock.
        harness.task = replace(
            harness.task,
            heartbeat_at=harness.clock.now - timedelta(seconds=10),
            lease_expires_at=harness.clock.now - timedelta(seconds=1),
        )
        harness.tasks._tasks[harness.task.id] = harness.task

    return expire


async def test_fence_replaced_between_phases_a_and_c_is_not_delivered() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.RUNNING)
    queued = await harness.enqueue(session, text="Fence race.", key="i-fence-3")
    harness.host.status_state = "awaiting_instruction"

    async def replace_fence() -> None:
        harness.replace_fence()

    harness.host.before_send = replace_fence
    report = await harness.delivery.drain_once()
    assert report.unknown == 1
    stored = await harness.instruction(queued.instruction.id)
    assert stored.state is InstructionState.DELIVERY_UNKNOWN
    assert stored.last_delivery_error == "lease_expired"


async def test_crash_between_phases_leaves_delivering_then_becomes_unknown() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    queued = await harness.seed_queued_instruction(session, text="Crash simulation.", key="i-crash")
    token = uuid4()
    reserved = queued.with_delivery_reserved(
        token=token,
        delivery_sha256=instruction_text_sha256("Crash simulation."),
        now=NOW - timedelta(seconds=GRACE_SECONDS + 60),
    )
    await harness.sessions.update_instruction_delivery(
        reserved, expected_state=InstructionState.QUEUED, expected_delivery_token=None
    )
    assert (await harness.instruction(queued.id)).state is InstructionState.DELIVERING

    report = await harness.delivery.drain_once()
    assert report.stale_unknown == 1
    recovered = await harness.instruction(queued.id)
    assert recovered.state is InstructionState.DELIVERY_UNKNOWN
    assert recovered.delivery_token == token
    assert recovered.last_delivery_error == "delivery_timeout"


# ------------------------------------------------------ capability / INS-10


async def test_unsupported_host_capability_keeps_the_queue_intact() -> None:
    harness = _Harness()
    session = await harness.seed_session(
        state=AgentSessionState.AWAITING_INSTRUCTION, capability=False
    )
    enqueued = await harness.enqueue(session, text="No capability.", key="i-cap")
    assert enqueued.delivered_live is False
    assert harness.host.send_calls == []

    report = await harness.delivery.drain_once()
    assert report.refused == 1
    assert harness.host.send_calls == []
    stored = await harness.instruction(enqueued.instruction.id)
    assert stored.state is InstructionState.QUEUED
    assert stored.last_delivery_error == "next_run_instruction_unsupported"


async def test_supervisor_restart_never_redelivers_or_loses_queue() -> None:
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    first = await harness.seed_queued_instruction(
        session,
        text="Already received.",
        key="i-10a",
        queued_at=NOW - timedelta(seconds=600),
    )
    second = await harness.seed_queued_instruction(
        session,
        text="Still queued.",
        key="i-10b",
        queued_at=NOW - timedelta(seconds=590),
    )
    harness.host.status_state = "awaiting_instruction"

    # The attempt reached the host, but a crash left the row ambiguous.
    token = uuid4()
    delivery_sha = instruction_text_sha256("Already received.")
    reserved = first.with_delivery_reserved(
        token=token,
        delivery_sha256=delivery_sha,
        now=NOW - timedelta(seconds=GRACE_SECONDS + 120),
    )
    await harness.sessions.update_instruction_delivery(
        reserved, expected_state=InstructionState.QUEUED, expected_delivery_token=None
    )
    unknown = reserved.with_state(
        InstructionState.DELIVERY_UNKNOWN,
        now=NOW - timedelta(seconds=GRACE_SECONDS + 60),
        last_delivery_error="host_transport_error",
    )
    await harness.sessions.update_instruction_delivery(
        unknown, expected_state=InstructionState.DELIVERING, expected_delivery_token=token
    )
    harness.host.events = [
        HostSessionEventV1(
            seq=1,
            kind="instruction_delivered",
            at=NOW,
            payload={
                "instruction_id": str(first.id),
                "sha256": delivery_sha,
                "mode": "next_run",
            },
        )
    ]

    # A brand-new service instance stands for a restarted backend process.
    restarted = InstructionDeliveryService(
        uow_factory=harness.uow_factory,
        host=harness.host,
        notes=harness.notes,
        disclosure=harness.disclosure,
        clock=harness.clock,
        grace_seconds=GRACE_SECONDS,
    )
    recovered = await restarted.drain_once()
    assert recovered.reconciled_delivered == 1
    assert recovered.delivered == 1
    # The ambiguous instruction is proven delivered from the journal without a
    # re-send; the still-queued instruction is delivered once, in order.
    assert (await harness.instruction(first.id)).state is InstructionState.DELIVERED
    assert (await harness.instruction(second.id)).state is InstructionState.DELIVERED
    assert len(harness.host.send_calls) == 1
    assert harness.host.send_calls[0]["request"].instruction_id == second.id


# ------------------------------------------------------------ ID-3 ack seam


async def test_delivery_then_pull_and_ack_report_the_persisted_delivery_text() -> None:
    """ID-3: the operator pulls and acknowledges exactly what ID-2 persisted."""
    harness = _Harness()
    session = await harness.seed_session(state=AgentSessionState.AWAITING_INSTRUCTION)
    diff = {
        "path": "notes/humaines/piste.md",
        "queued_revision_id": "rev-1",
        "queued_sha256": "1" * 64,
        "current_revision_id": "rev-2",
        "current_sha256": "2" * 64,
    }
    delivery_text = (
        "Follow the note.\n[notes modifiées depuis la mise en file]\n"
        "- notes/humaines/piste.md: rev-1 -> rev-2"
    )
    harness.notes.diffs = (diff,)
    harness.notes.delivery_text = delivery_text
    enqueued = await harness.enqueue(
        session,
        text="Follow the note.",
        key="i-ack",
        note_refs=("notes/humaines/piste.md",),
    )
    assert enqueued.instruction.state is InstructionState.DELIVERED

    operator = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1")
    pending = await harness.service.pull_delivered_instructions(
        INVESTIGATION_ID, session.id, actor=operator
    )
    assert len(pending) == 1
    assert pending[0].instruction.id == enqueued.instruction.id
    assert pending[0].delivery_text == delivery_text
    assert pending[0].instruction.delivery_sha256 == instruction_text_sha256(delivery_text)
    assert pending[0].delivery_text_verified is True

    acknowledged = await harness.service.acknowledge_instructions_with_notes(
        INVESTIGATION_ID,
        session.id,
        instruction_ids=(enqueued.instruction.id,),
        actor=operator,
    )
    assert acknowledged[0].instruction.state is InstructionState.ACKNOWLEDGED
    assert acknowledged[0].delivery_text == delivery_text
    assert dict(acknowledged[0].note_diffs[0]) == diff

    assert (
        await harness.service.pull_delivered_instructions(
            INVESTIGATION_ID, session.id, actor=operator
        )
        == ()
    )

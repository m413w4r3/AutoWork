"""Oracles for B2: host teardown requested after a lease release.

The release service runs *after* the command transaction committed, so these
tests assert the protocol rather than PostgreSQL: deterministic close keys,
the interrupt-then-close path the host requires for a running run, tolerance to
a missing or unreachable host agent, and the durable ``lost_unreconciled``
marker that keeps an unconfirmed effect observable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from cti_app.application.agentic_host_sessions import (
    HostBudgetUsageV1,
    HostCloseResultV1,
    HostInterruptResultV1,
    HostSessionsPort,
    HostSessionStateConflictError,
    HostSessionStatusV1,
)
from cti_app.application.agentic_sessions import HostSessionReleaseService, SessionUnitOfWork
from cti_app.application.investigation_persistence import (
    InMemoryAgentSessionRepository,
    InMemoryInvestigationTaskRepository,
)
from cti_app.application.investigation_task_events import TaskEventType
from cti_app.application.investigation_tasks import TaskSessionOutcome, TaskSessionRelease
from cti_app.application.workspace_agent import WorkspaceLocalUnavailableError
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
    SessionReconcileOutcome,
)
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
EXECUTABLE = "/usr/local/bin/codex"


def _capabilities() -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable=EXECUTABLE,
        version="codex-cli 1.2.3",
        modes=("exec",),
        supports_resume=False,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        permission_modes=("workspace-write",),
        probed_at=NOW,
        probes=(
            OperatorProbeCommand(
                argv=(EXECUTABLE, "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "workspace-write"),
                resolved_version="codex-cli 1.2.3",
            ),
        ),
    )


class _Uow:
    def __init__(
        self,
        sessions: InMemoryAgentSessionRepository,
        tasks: InMemoryInvestigationTaskRepository | None = None,
    ) -> None:
        self.agent_sessions = sessions
        self.investigation_tasks = tasks or InMemoryInvestigationTaskRepository()
        self.commits = 0

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


async def _closed_session(
    sessions: InMemoryAgentSessionRepository,
    *,
    host_session_ref: str | None = "host-session-1",
    terminal: bool = True,
) -> AgentSessionV1:
    capabilities = _capabilities()
    session = AgentSessionV1(
        id=uuid4(),
        investigation_id=uuid4(),
        task_id=uuid4(),
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.PROVISIONING,
        state_version=1,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=NOW,
        auth_secret_nonce=b"\x01" * 32,
        auth_secret_hash="b" * 64,
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"session-{uuid4()}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=host_session_ref,
    )
    stored, _created = await sessions.add(session)
    if terminal:
        ready = await sessions.update_state(
            stored.with_state(AgentSessionState.READY, now=NOW),
            expected_state_version=stored.state_version,
        )
        assert ready is not None
        await sessions.close_session(
            session_id=stored.id,
            investigation_id=stored.investigation_id,
            task_id=stored.task_id,
            outcome="closed",
            reason="submitted",
        )
    current = await sessions.get(stored.id)
    assert current is not None
    return current


def _release(session: AgentSessionV1) -> TaskSessionRelease:
    return TaskSessionRelease(
        session_id=session.id,
        investigation_id=session.investigation_id,
        task_id=session.task_id,
        outcome=TaskSessionOutcome.CLOSED,
        reason="submitted",
    )


_HostState = Literal[
    "provisioning",
    "ready",
    "running",
    "awaiting_instruction",
    "interrupt_requested",
    "stopped",
    "lost",
    "reconciling",
    "closed",
    "failed",
]


class _Host:
    def __init__(
        self,
        *,
        close_error: Exception | None = None,
        states: tuple[_HostState, ...] = ("stopped",),
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._close_error = close_error
        self._states = list(states)

    def _status(self) -> HostSessionStatusV1:
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return HostSessionStatusV1(
            state=state,
            state_version=1,
            adapter_kind="codex_cli",
            budgets_used=HostBudgetUsageV1(wall_seconds=0, output_bytes=0, instructions=0),
            last_activity_at=NOW,
            log_cursor=0,
        )

    async def close_session(
        self, session_id: UUID, request: Any, *, idempotency_key: str
    ) -> HostCloseResultV1:
        self.calls.append(("close", idempotency_key))
        if self._close_error is not None:
            error, self._close_error = self._close_error, None
            raise error
        return HostCloseResultV1(state="closed")

    async def interrupt_session(
        self, session_id: UUID, request: Any, *, idempotency_key: str
    ) -> HostInterruptResultV1:
        self.calls.append(("interrupt", idempotency_key))
        return HostInterruptResultV1(state="interrupt_requested", signal_sent=True)

    async def get_session_status(self, session_id: UUID) -> HostSessionStatusV1:
        self.calls.append(("status", str(session_id)))
        return self._status()


def _service(
    sessions: InMemoryAgentSessionRepository,
    host: _Host | None,
    *,
    wait_seconds: float = 0.0,
    tasks: InMemoryInvestigationTaskRepository | None = None,
):
    return HostSessionReleaseService(
        uow_factory=cast(Callable[[], SessionUnitOfWork], lambda: _Uow(sessions, tasks)),
        host=cast(HostSessionsPort, host) if host is not None else None,
        wait_seconds=wait_seconds,
        sleep=lambda _seconds: _noop(),
        monotonic=lambda: 0.0,
    )


async def _noop() -> None:
    return None


async def test_release_closes_the_host_with_a_deterministic_key() -> None:
    sessions = InMemoryAgentSessionRepository()
    session = await _closed_session(sessions)
    host = _Host()

    await _service(sessions, host).flush([_release(session), _release(session)])

    assert [name for name, _key in host.calls] == ["close"]
    assert host.calls[0][1] == f"task-release.close.{session.id}"
    stored = await sessions.get(session.id)
    assert stored is not None and stored.reconciliation is None


async def test_release_interrupts_then_closes_a_run_the_host_refuses_to_close() -> None:
    sessions = InMemoryAgentSessionRepository()
    session = await _closed_session(sessions)
    host = _Host(
        close_error=HostSessionStateConflictError("the host run is still active"),
        states=("running", "stopped"),
    )

    await _service(sessions, host, wait_seconds=1.0).flush([_release(session)])

    assert [name for name, _key in host.calls] == [
        "close",
        "interrupt",
        "status",
        "status",
        "close",
    ]
    assert host.calls[1][1] == f"task-release.interrupt.{session.id}"
    stored = await sessions.get(session.id)
    assert stored is not None and stored.reconciliation is None


async def test_release_marks_a_stuck_host_as_lost_unreconciled() -> None:
    sessions = InMemoryAgentSessionRepository()
    session = await _closed_session(sessions)
    host = _Host(
        close_error=HostSessionStateConflictError("the host run is still active"),
        states=("running",),
    )

    await _service(sessions, host, wait_seconds=0.0).flush([_release(session)])

    assert [name for name, _key in host.calls] == ["close", "interrupt", "status"]
    stored = await sessions.get(session.id)
    assert stored is not None
    assert stored.reconciliation is not None
    assert stored.reconciliation_outcome() is SessionReconcileOutcome.LOST_UNRECONCILED
    assert "did not stop" in str(stored.reconciliation["detail"])
    assert stored.reconciliation_sha256 is not None


async def test_missing_host_runtime_is_marked_and_never_raises() -> None:
    sessions = InMemoryAgentSessionRepository()
    session = await _closed_session(sessions)

    await _service(sessions, None).flush([_release(session)])

    stored = await sessions.get(session.id)
    assert stored is not None
    assert stored.reconciliation_outcome() is SessionReconcileOutcome.LOST_UNRECONCILED
    assert "not wired" in str(stored.reconciliation["detail"])


async def test_unreachable_host_is_marked_and_never_raises() -> None:
    sessions = InMemoryAgentSessionRepository()
    session = await _closed_session(sessions)
    host = _Host(close_error=WorkspaceLocalUnavailableError("workspace agent is unreachable"))

    await _service(sessions, host).flush([_release(session)])

    stored = await sessions.get(session.id)
    assert stored is not None
    assert stored.reconciliation_outcome() is SessionReconcileOutcome.LOST_UNRECONCILED
    assert "unreachable" in str(stored.reconciliation["detail"]).lower() or (
        "WorkspaceLocalUnavailableError" in str(stored.reconciliation["detail"])
    )


async def test_session_without_host_reference_has_nothing_to_stop() -> None:
    sessions = InMemoryAgentSessionRepository()
    session = await _closed_session(sessions, host_session_ref=None)
    host = _Host()

    await _service(sessions, host).flush([_release(session)])

    assert host.calls == []
    stored = await sessions.get(session.id)
    assert stored is not None and stored.reconciliation is None


async def test_confirmed_release_appends_one_durable_confirmation_event() -> None:
    """MAJOR 3: a crash between commit and flush must remain detectable."""
    sessions = InMemoryAgentSessionRepository()
    session = await _closed_session(sessions)
    tasks = InMemoryInvestigationTaskRepository()
    await tasks.add(
        InvestigationTaskV1(
            id=session.task_id,
            investigation_id=session.investigation_id,
            task_key="lead-release",
            title="Inspect the loader",
            question="",
            objective="",
            priority=TaskPriority.NORMAL,
            status=TaskStatus.RUNNING,
            status_version=3,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
            created_at=NOW,
            updated_at=NOW,
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id="codex:operator-1",
        )
    )
    host = _Host()

    await _service(sessions, host, tasks=tasks).flush([_release(session)])

    events = await tasks.list_events(session.task_id)
    assert [event.event_type for event in events] == [TaskEventType.SESSION_STATE_CHANGED]
    assert events[0].idempotency_key == f"host-release:{session.id}"
    assert events[0].payload["action"] == "host_session_released"

    # An API flush and the sweep recovery may both replay the same release.
    await _service(sessions, host, tasks=tasks).flush([_release(session)])
    assert len(await tasks.list_events(session.task_id)) == 1
    stored = await sessions.get(session.id)
    assert stored is not None and stored.reconciliation is None

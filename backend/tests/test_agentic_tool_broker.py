"""M24-05B2 broker oracles (ADR-0011 D7/D8/D10 D04/D09, M3).

The broker composes the real :class:`OperatorToolInvocationService` (lot 04B2/05B1
machinery stays the only journal/budget/evidence path) over the in-memory
fixtures, so these tests exercise the real idempotency prefix, the real task
engine and the real dispatcher instead of a mock journal.  The route section
drives the actual D7 HMAC verifier over a real ``AgentSessionV1`` row.

No live provider is contacted: every tool adapter is the in-memory fake from
``tests.test_investigation_service``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.agentic_tools import router as agentic_tools_router
from cti_app.application.agent_session_verifier import (
    HmacSessionSecretDeriver,
    VerifiedAgentSession,
    build_agent_session_message,
    session_secret_hash,
)
from cti_app.application.agentic_tool_broker import (
    AGENTIC_TOOL_NAMES,
    BRIDGE_CONSULT_ALIAS,
    AgenticArgumentsInvalidError,
    AgenticBindingRefusedError,
    AgenticLeaseRefusedError,
    AgenticProseAuthorityRequiredError,
    AgenticSessionInactiveError,
    AgenticToolBroker,
    AgenticToolNotAllowedError,
    agentic_idempotency_key,
)
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.investigation_handoffs import InvestigationHandoffService
from cti_app.application.investigation_persistence import (
    InMemoryAgentSessionRepository,
    InMemoryInvestigationTaskRepository,
)
from cti_app.application.operator_tool_invocation import OperatorToolInvocationError
from cti_app.config import Settings
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
)
from cti_app.domain.investigation import (
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)
from cti_app.domain.production import ProseAuthority
from tests.test_investigation_service import _Fixture
from tests.test_operator_tool_invocation import _service

NOW = datetime.now(UTC)
SESSION_KEY = "m" * 32
OPERATOR_ID = "operator:test"
_ARGUMENTS = {"query": "sample hash"}


def _fixture(*, external_allowed: bool = True) -> _Fixture:
    return _Fixture(
        responses=[],
        external_allowed=external_allowed,
        tools=(("vt.file_report", "vt"),),
    )


def _advisor_fixture() -> _Fixture:
    return _Fixture(responses=[], tools=(("advisor.consult", "bridge"),))


def _running_task(
    investigation_id: UUID,
    session_id: UUID,
    *,
    fence: int = 1,
    expires_at: datetime | None = None,
) -> InvestigationTaskV1:
    return InvestigationTaskV1(
        investigation_id=investigation_id,
        task_key=f"lead-{uuid4().hex[:8]}",
        title="Inspect the loader",
        question="Which gate contacts the C2?",
        objective="Identify the second-stage downloader.",
        priority=TaskPriority.HIGH,
        status=TaskStatus.RUNNING,
        status_version=3,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=OPERATOR_ID,
        lease_id=uuid4(),
        lease_session_id=session_id,
        lease_fence=fence,
        lease_expires_at=expires_at or NOW + timedelta(seconds=180),
        heartbeat_at=NOW,
    )


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


def _session_row(
    investigation_id: UUID,
    task: InvestigationTaskV1,
    *,
    session_id: UUID | None = None,
) -> tuple[AgentSessionV1, bytes]:
    session_id = session_id or uuid4()
    nonce = b"s" * 32
    secret = HmacSessionSecretDeriver(SESSION_KEY).derive(session_id=session_id, nonce=nonce)
    capabilities = _capabilities()
    row = AgentSessionV1(
        id=session_id,
        investigation_id=investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.RUNNING,
        state_version=2,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=nonce,
        auth_secret_hash=session_secret_hash(secret),
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"session-{session_id}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
        task_lease_id=task.lease_id,
        task_lease_fence=task.lease_fence,
    )
    return row, secret


def _verified(
    session_id: UUID,
    investigation_id: UUID,
    task: InvestigationTaskV1,
    *,
    fence: int | None = None,
) -> VerifiedAgentSession:
    return VerifiedAgentSession(
        session_id=session_id,
        investigation_id=investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        operator_id=OPERATOR_ID,
        task_lease_fence=task.lease_fence if fence is None else fence,
    )


async def _seed_agent_session(
    fixture: _Fixture,
    tasks: InMemoryInvestigationTaskRepository,
    sessions: InMemoryAgentSessionRepository,
    *,
    fence: int = 1,
    expires_at: datetime | None = None,
) -> tuple[InvestigationTaskV1, UUID, bytes]:
    """A running leased task plus its active session row and secret."""
    session_id = uuid4()
    task = _running_task(fixture.investigation_id, session_id, fence=fence, expires_at=expires_at)
    await tasks.add(task)
    row, secret = _session_row(fixture.investigation_id, task, session_id=session_id)
    await sessions.add(row)
    return task, session_id, secret


class _BrokerUnitOfWork:
    """The canonical stores the broker composes; the API route enters it."""

    def __init__(
        self,
        fixture: _Fixture,
        tasks: InMemoryInvestigationTaskRepository,
        sessions: InMemoryAgentSessionRepository,
    ) -> None:
        self.investigation_tasks = tasks
        self.investigation_states = fixture.states
        self.analyst_decisions = fixture.analyst_decisions
        self.agent_sessions = sessions
        self.analyst_investigations = fixture.investigations
        self.commit_count = 0

    async def __aenter__(self) -> _BrokerUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


def _broker(
    fixture: _Fixture,
    tasks: InMemoryInvestigationTaskRepository,
    sessions: InMemoryAgentSessionRepository,
    *,
    prose_authority: ProseAuthority = ProseAuthority.BRIDGE_ONLY,
    handoffs: InvestigationHandoffService | None = None,
) -> tuple[AgenticToolBroker, _BrokerUnitOfWork]:
    uow = _BrokerUnitOfWork(fixture, tasks, sessions)
    broker = AgenticToolBroker(
        investigation_id=fixture.investigation_id,
        prose_authority=prose_authority,
        uow=uow,
        invocations=_service(fixture),
        handoffs=handoffs,
        task_limits=TASK_LIMIT_DEFAULTS,
        clock=lambda: NOW,
        commit=uow.commit,
    )
    return broker, uow


async def _invocations(fixture: _Fixture) -> tuple[ToolInvocation, ...]:
    return cast(
        tuple[ToolInvocation, ...],
        await fixture.invocations.list_for_investigation(fixture.investigation_id),
    )


# ---------------------------------------------------------------- D04 oracles


@pytest.mark.asyncio
async def test_broker_delegates_invocation_journal_and_attributes_task_session() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)

    result = await broker.invoke(
        session=_verified(session_id, fixture.investigation_id, task),
        tool_name="vt.file_report",
        schema_version="1",
        arguments=_ARGUMENTS,
        client_idempotency_key="client-1",
    )

    assert result.status == ToolInvocationStatus.SUCCEEDED.value
    assert result.invocation_id is not None
    rows = await _invocations(fixture)
    assert len(rows) == 1
    assert rows[0].task_id == task.id
    assert rows[0].agent_session_id == session_id
    assert rows[0].actor_kind is ToolInvocationActorKind.OPERATOR
    assert rows[0].actor_id == OPERATOR_ID
    assert fixture.tools["vt.file_report"].calls == [_ARGUMENTS]


@pytest.mark.asyncio
async def test_broker_refuses_missing_or_stale_lease() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)

    async def attempt(session: VerifiedAgentSession, key: str) -> None:
        with pytest.raises(AgenticLeaseRefusedError) as refused:
            await broker.invoke(
                session=session,
                tool_name="vt.file_report",
                schema_version="1",
                arguments=_ARGUMENTS,
                client_idempotency_key=key,
            )
        assert refused.value.code == "agent_session_lease_stale"
        assert refused.value.status_code == 409

    # Superseded by another session.
    tasks._tasks[task.id] = _replace_task(task, lease_session_id=uuid4())
    await attempt(_verified(session_id, fixture.investigation_id, task), "lease-session")
    # The session's recorded fence is no longer the task's fence.
    tasks._tasks[task.id] = _replace_task(task, lease_fence=task.lease_fence + 1)
    await attempt(_verified(session_id, fixture.investigation_id, task), "lease-fence")
    # The lease deadline passed while the session stayed alive.
    tasks._tasks[task.id] = _replace_task(
        task,
        heartbeat_at=NOW - timedelta(seconds=400),
        lease_expires_at=NOW - timedelta(seconds=1),
    )
    await attempt(_verified(session_id, fixture.investigation_id, task), "lease-expired")

    assert await _invocations(fixture) == ()
    assert fixture.tools["vt.file_report"].calls == []


def _replace_task(task: InvestigationTaskV1, **changes: object) -> InvestigationTaskV1:
    return replace(task, **changes)


def test_broker_key_includes_task_and_fence() -> None:
    session_id = uuid4()
    task_id = uuid4()
    key = agentic_idempotency_key(
        session_id=session_id, task_id=task_id, lease_fence=1, client_key="client-key"
    )

    assert key == f"agent:{session_id}:{task_id}:1:client-key"
    assert key == agentic_idempotency_key(
        session_id=session_id, task_id=task_id, lease_fence=1, client_key="client-key"
    )
    assert key != agentic_idempotency_key(
        session_id=session_id, task_id=task_id, lease_fence=2, client_key="client-key"
    )
    assert key != agentic_idempotency_key(
        session_id=session_id, task_id=uuid4(), lease_fence=1, client_key="client-key"
    )
    assert key != agentic_idempotency_key(
        session_id=session_id, task_id=task_id, lease_fence=1, client_key="other"
    )
    with pytest.raises(AgenticArgumentsInvalidError):
        agentic_idempotency_key(
            session_id=session_id, task_id=task_id, lease_fence=1, client_key="   "
        )
    with pytest.raises(AgenticArgumentsInvalidError):
        agentic_idempotency_key(
            session_id=session_id,
            task_id=task_id,
            lease_fence=1,
            client_key="x" * 255,
        )


# --------------------------------------------------------- profile and replay


@pytest.mark.asyncio
async def test_broker_refuses_opinion_tools_server_side() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)

    for tool_name in ("opinion.compose", "opinion.revise", "opinion.note"):
        with pytest.raises(AgenticProseAuthorityRequiredError) as refused:
            await broker.invoke(
                session=_verified(session_id, fixture.investigation_id, task),
                tool_name=tool_name,
                schema_version="1",
                arguments={"text": "operator prose must never publish"},
                client_idempotency_key=f"opinion-{tool_name}",
            )
        assert refused.value.code == "bridge_authorship_required"
        assert refused.value.status_code == 403

    assert await _invocations(fixture) == ()


@pytest.mark.asyncio
async def test_broker_refuses_tool_outside_catalogue() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)

    for tool_name in ("local.run_command", "shell.exec", "finding.propose"):
        with pytest.raises(AgenticToolNotAllowedError) as refused:
            await broker.invoke(
                session=_verified(session_id, fixture.investigation_id, task),
                tool_name=tool_name,
                schema_version="1",
                arguments={"command": "id"},
                client_idempotency_key=f"forbidden-{tool_name}",
            )
        assert refused.value.code == "agentic_tool_not_allowed"

    assert await _invocations(fixture) == ()


@pytest.mark.asyncio
async def test_broker_replays_same_client_key_without_second_dispatch() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)
    session = _verified(session_id, fixture.investigation_id, task)

    first = await broker.invoke(
        session=session,
        tool_name="vt.file_report",
        schema_version="1",
        arguments=_ARGUMENTS,
        client_idempotency_key="same-client-key",
    )
    replay = await broker.invoke(
        session=session,
        tool_name="vt.file_report",
        schema_version="1",
        arguments=_ARGUMENTS,
        client_idempotency_key="same-client-key",
    )

    assert replay.invocation_id == first.invocation_id
    assert replay.idempotent_replay is True
    assert len(await _invocations(fixture)) == 1
    # One G2 reservation, one dispatch: the replay never re-executed the tool.
    assert fixture.tools["vt.file_report"].calls == [_ARGUMENTS]


@pytest.mark.asyncio
async def test_broker_changed_fence_cannot_replay_older_invocation() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)
    session = _verified(session_id, fixture.investigation_id, task)

    first = await broker.invoke(
        session=session,
        tool_name="vt.file_report",
        schema_version="1",
        arguments=_ARGUMENTS,
        client_idempotency_key="shared-client-key",
    )
    updated = _replace_task(
        task,
        lease_fence=task.lease_fence + 1,
        lease_expires_at=NOW + timedelta(seconds=180),
    )
    tasks._tasks[task.id] = updated
    second = await broker.invoke(
        session=_verified(session_id, fixture.investigation_id, updated),
        tool_name="vt.file_report",
        schema_version="1",
        arguments=_ARGUMENTS,
        client_idempotency_key="shared-client-key",
    )

    assert second.idempotent_replay is False
    assert second.invocation_id != first.invocation_id
    assert len(await _invocations(fixture)) == 2


# -------------------------------------------------------- G2 and reconciliation


@pytest.mark.asyncio
async def test_broker_propagates_g2_refusal_without_provider_call() -> None:
    fixture = _fixture(external_allowed=False)
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)

    with pytest.raises(OperatorToolInvocationError) as refused:
        await broker.invoke(
            session=_verified(session_id, fixture.investigation_id, task),
            tool_name="vt.file_report",
            schema_version="1",
            arguments=_ARGUMENTS,
            client_idempotency_key="g2-denied",
        )

    assert refused.value.code == "external_disclosure_denied"
    assert refused.value.status_code == 403
    assert fixture.tools["vt.file_report"].calls == []
    assert await _invocations(fixture) == ()


@pytest.mark.asyncio
async def test_broker_never_resubmits_ambiguous_advisor_consult() -> None:
    fixture = _advisor_fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)
    session = _verified(session_id, fixture.investigation_id, task)
    arguments = {
        "consultation_type": "plan_investigation",
        "question": "Which independent check comes next?",
    }

    first = await broker.invoke(
        session=session,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments=arguments,
        client_idempotency_key="consult-a",
    )
    rows = await _invocations(fixture)
    assert len(rows) == 1
    # A concurrent first transport attempt left the same semantic call RUNNING:
    # its outcome is unknown, so a second client key must not resubmit it.
    assert await fixture.invocations.add_if_absent(
        ToolInvocation(
            investigation_id=fixture.investigation_id,
            operation_key="operator:ambiguous-advisor:",
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            normalized_parameters=arguments,
            purpose="ambiguous transport fixture",
            status=ToolInvocationStatus.RUNNING,
        )
    )

    with pytest.raises(OperatorToolInvocationError) as refused:
        await broker.invoke(
            session=session,
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            arguments=arguments,
            client_idempotency_key="consult-b",
        )

    assert refused.value.code == "ambiguous_tool_invocation_requires_reconciliation"
    # The refused call added no journal row and no second dispatch.
    assert len(await _invocations(fixture)) == 2
    assert fixture.tools["advisor.consult"].calls == [arguments]
    assert first.invocation_id is not None


@pytest.mark.asyncio
async def test_broker_refuses_expired_lease_mid_call() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)
    session = _verified(session_id, fixture.investigation_id, task)

    await broker.invoke(
        session=session,
        tool_name="vt.file_report",
        schema_version="1",
        arguments=_ARGUMENTS,
        client_idempotency_key="before-expiry",
    )
    # The lease expires between two calls of the same live session.
    tasks._tasks[task.id] = _replace_task(
        task,
        heartbeat_at=NOW - timedelta(seconds=400),
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(AgenticLeaseRefusedError):
        await broker.invoke(
            session=session,
            tool_name="vt.file_report",
            schema_version="1",
            arguments=_ARGUMENTS,
            client_idempotency_key="after-expiry",
        )

    assert len(await _invocations(fixture)) == 1
    assert fixture.tools["vt.file_report"].calls == [_ARGUMENTS]


# ------------------------------------------------------------ mapped services


@pytest.mark.asyncio
async def test_broker_maps_task_commands_to_the_engine() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, uow = _broker(fixture, tasks, sessions)
    verified = _verified(session_id, fixture.investigation_id, task)

    heartbeat = await broker.invoke(
        session=verified,
        tool_name="task.heartbeat",
        schema_version="1",
        arguments={"expected_status_version": 3, "lease_fence": 1},
        client_idempotency_key="heartbeat-1",
    )
    assert heartbeat.status == TaskStatus.RUNNING.value
    stored = await tasks.get(task.id)
    assert stored is not None
    assert stored.heartbeat_at is not None
    assert uow.commit_count >= 1

    submitted = await broker.invoke(
        session=verified,
        tool_name="task.submit_result",
        schema_version="1",
        arguments={
            "expected_status_version": 3,
            "lease_fence": 1,
            "result_refs": [{"id": str(uuid4()), "kind": "observation"}],
        },
        client_idempotency_key="submit-1",
    )
    assert submitted.status == TaskStatus.AWAITING_REVIEW.value
    stored = await tasks.get(task.id)
    assert stored is not None
    assert stored.status is TaskStatus.AWAITING_REVIEW
    assert stored.lease_id is None
    closed = await sessions.get(session_id)
    assert closed is not None
    assert closed.state is AgentSessionState.CLOSED


@pytest.mark.asyncio
async def test_broker_maps_handoff_submit_to_the_session_task() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)

    calls: list[dict[str, Any]] = []

    class _FakeHandoffs:
        async def create(self, investigation_id: UUID, **kwargs: Any) -> Any:
            calls.append({"investigation_id": investigation_id, **kwargs})
            return SimpleNamespace(
                handoff=SimpleNamespace(
                    to_payload=lambda: {"handoff_id": "handoff-1", "task_id": str(task.id)}
                )
            )

    broker, _uow = _broker(
        fixture,
        tasks,
        sessions,
        handoffs=cast(InvestigationHandoffService, _FakeHandoffs()),
    )

    result = await broker.invoke(
        session=_verified(session_id, fixture.investigation_id, task),
        tool_name="handoff.submit",
        schema_version="investigation-handoff.v1",
        arguments={
            "investigation_id": str(fixture.investigation_id),
            "task_id": str(task.id),
            "to_actor": {"kind": "human", "actor_id": "analyst:test"},
            "next_action": "Review the loader result.",
        },
        client_idempotency_key="handoff-1",
    )

    assert result.status == "handoff_recorded"
    assert len(calls) == 1
    assert calls[0]["investigation_id"] == fixture.investigation_id
    assert calls[0]["request"].task_id == task.id
    assert calls[0]["actor"].kind.value == "operator"
    assert calls[0]["actor"].actor_id == OPERATOR_ID
    assert calls[0]["idempotency_key"].startswith(f"agent:{session_id}:{task.id}:")


@pytest.mark.asyncio
async def test_broker_refuses_handoff_for_another_task() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)

    with pytest.raises(AgenticBindingRefusedError) as refused:
        await broker.invoke(
            session=_verified(session_id, fixture.investigation_id, task),
            tool_name="handoff.submit",
            schema_version="investigation-handoff.v1",
            arguments={
                "task_id": str(uuid4()),
                "to_actor": {"kind": "human", "actor_id": "analyst:test"},
                "next_action": "Review another task.",
            },
            client_idempotency_key="handoff-wrong-task",
        )

    assert refused.value.code == "agent_session_binding_mismatch"


# ------------------------------------------------------------------ contract


def test_broker_catalogue_matches_the_host_shim_catalogue() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from sidecars.workspace_agent.sessions import catalog

    shim_names = {spec.broker_name for spec in catalog.CATALOGUE}
    assert shim_names == set(AGENTIC_TOOL_NAMES) - {BRIDGE_CONSULT_ALIAS}
    assert BRIDGE_CONSULT_ALIAS not in shim_names


# ------------------------------------------------------------- signed routes


class _OperatorClientIdentity:
    async def current(self) -> Identity:
        return Identity(
            actor_id="operator:operator-client",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


class _Composition:
    def __init__(self, service: Any) -> None:
        self.operator_tool_invocations = service

    def build(
        self, _uow: Any, _investigation_id: UUID, *, operator_mode: bool = False
    ) -> _Composition:
        del operator_mode
        return self


def _route_app(
    fixture: _Fixture,
    uow: _BrokerUnitOfWork,
    *,
    key: str | None = SESSION_KEY,
) -> FastAPI:
    app = FastAPI()
    app.include_router(agentic_tools_router)
    app.state.uow_factory = lambda: uow
    app.state.settings = Settings(_env_file=None, m24_agent_session_key=key)
    app.state.investigation_composition_factory = _Composition(_service(fixture))
    app.state.identity_provider = _OperatorClientIdentity()
    return app


def _signed_headers(
    secret: bytes,
    *,
    path: str,
    session_id: UUID,
    body: bytes,
    idempotency_key: str,
) -> dict[str, str]:
    timestamp = str(int(datetime.now(UTC).timestamp()))
    message = build_agent_session_message(
        method="POST",
        path_and_query=path,
        session_id=session_id,
        body=body,
        idempotency_key=idempotency_key,
        timestamp=timestamp,
    )
    digest = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return {
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
        "X-AutoWork-Agent-Session": str(session_id),
        "X-AutoWork-Agent-Timestamp": timestamp,
        "X-AutoWork-Agent-Signature": f"v1={digest}",
    }


@pytest.mark.asyncio
async def test_signed_tool_call_route_attributes_and_refuses_token_alone() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task = _running_task(fixture.investigation_id, uuid4())
    session_row, secret = _session_row(fixture.investigation_id, task)
    task = _replace_task(task, lease_session_id=session_row.id)
    await tasks.add(task)
    await sessions.add(session_row)
    uow = _BrokerUnitOfWork(fixture, tasks, sessions)
    app = _route_app(fixture, uow)

    path = (
        f"/api/investigations/{fixture.investigation_id}/agent-sessions/{session_row.id}/tool-calls"
    )
    payload = {
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "arguments": _ARGUMENTS,
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(
            path,
            content=body,
            headers={"Idempotency-Key": "token-alone", "Content-Type": "application/json"},
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["code"] == "agent_session_required"

        accepted = await client.post(
            path,
            content=body,
            headers=_signed_headers(
                secret,
                path=path,
                session_id=session_row.id,
                body=body,
                idempotency_key="signed-1",
            ),
        )

    assert accepted.status_code == 200, accepted.text
    body_json = accepted.json()
    assert body_json["session_id"] == str(session_row.id)
    assert body_json["task_id"] == str(task.id)
    assert body_json["idempotent_replay"] is False
    rows = await _invocations(fixture)
    assert len(rows) == 1
    assert rows[0].task_id == task.id
    assert rows[0].agent_session_id == session_row.id


@pytest.mark.asyncio
async def test_tool_call_route_is_fail_closed_without_server_key() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    task = _running_task(fixture.investigation_id, uuid4())
    session_row, secret = _session_row(fixture.investigation_id, task)
    task = _replace_task(task, lease_session_id=session_row.id)
    await tasks.add(task)
    await sessions.add(session_row)
    uow = _BrokerUnitOfWork(fixture, tasks, sessions)
    app = _route_app(fixture, uow, key=None)

    path = (
        f"/api/investigations/{fixture.investigation_id}/agent-sessions/{session_row.id}/tool-calls"
    )
    body = json.dumps(
        {"tool_name": "vt.file_report", "schema_version": "1", "arguments": _ARGUMENTS},
        separators=(",", ":"),
    ).encode("utf-8")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(
            path,
            content=body,
            headers=_signed_headers(
                secret,
                path=path,
                session_id=session_row.id,
                body=body,
                idempotency_key="no-key",
            ),
        )

    assert refused.status_code == 503, refused.text
    assert refused.json()["detail"]["code"] == "agent_sessions_unavailable"
    assert fixture.tools["vt.file_report"].calls == []


@pytest.mark.asyncio
async def test_broker_refuses_an_inactive_or_unbound_session_row() -> None:
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    session_id = uuid4()
    task = _running_task(fixture.investigation_id, session_id)
    await tasks.add(task)
    broker, _uow = _broker(fixture, tasks, sessions)

    # The task lease names this session, but no active session row exists.
    with pytest.raises(AgenticSessionInactiveError) as refused:
        await broker.invoke(
            session=_verified(session_id, fixture.investigation_id, task),
            tool_name="vt.file_report",
            schema_version="1",
            arguments=_ARGUMENTS,
            client_idempotency_key="no-session-row",
        )

    assert refused.value.code == "agent_session_inactive"
    assert await _invocations(fixture) == ()

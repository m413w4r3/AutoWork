"""Acceptance gap tests for the ADR-0011 A01-F10 matrix.

Only D06 needed one. Its one-line summary ("interruption : versions conservées,
effets déjà partis réconciliés") has no single owner-lot test: the session
suite proves the persisted stop, the transfer suite proves that analyses and
versions survive a transfer interrupt, and the broker suite proves that a sent
effect is replayed from its journal instead of being dispatched twice.  This
test composes the existing in-memory harnesses of those suites so the exact
scenario has one deterministic, offline oracle.

No application code is exercised differently: the harnesses are the canonical
fakes of lots 05A/05B1/05B2 and no provider, network or process is contacted.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cti_app.application.agentic_tool_broker import AgenticLeaseRefusedError
from cti_app.application.investigation_persistence import (
    InMemoryAgentSessionRepository,
    InMemoryInvestigationTaskRepository,
)
from cti_app.domain.agentic_operator import AgentSessionState
from cti_app.domain.investigation import InvestigationExecutionMode
from tests.test_agent_sessions import HUMAN_ACTOR
from tests.test_agent_sessions import _Harness as _SessionHarness
from tests.test_agentic_tool_broker import (
    _ARGUMENTS,
    _broker,
    _fixture,
    _invocations,
    _seed_agent_session,
    _verified,
)


@pytest.mark.asyncio
async def test_d06_interrupt_keeps_persisted_versions_and_reconciles_sent_effects() -> None:
    # 1. Interrupting a running session persists a new state version and never
    #    deletes or rewrites the session row; the task and its lease are not
    #    touched by the interrupt itself.
    harness = _SessionHarness()
    task = await harness.seed_task()
    prepared = await harness.open(task=task)
    task_before = harness.task_row(task.id)
    stopped = await harness.service.interrupt(
        harness.investigation_id,
        prepared.session.id,
        reason="human stop",
        actor=HUMAN_ACTOR,
    )

    assert stopped.id == prepared.session.id
    assert stopped.state is AgentSessionState.STOPPED
    # RUNNING -> INTERRUPT_REQUESTED -> STOPPED: both CAS writes are kept.
    assert stopped.state_version == prepared.session.state_version + 2
    stored = await harness.sessions.get(prepared.session.id)
    assert stored is not None
    assert stored.state is AgentSessionState.STOPPED
    assert stored.state_version == stopped.state_version
    assert len(harness.operator.interrupted) == 1
    after_task = harness.task_row(task.id)
    assert (
        after_task.status,
        after_task.status_version,
        after_task.lease_fence,
        after_task.lease_session_id,
    ) == (
        task_before.status,
        task_before.status_version,
        task_before.lease_fence,
        task_before.lease_session_id,
    )

    # 2. An effect sent before the interrupt stays in the journal exactly once;
    #    its replay is idempotent.  Once the lease moved (the interrupt path
    #    releases it), the same session can no longer send a new effect and no
    #    second dispatch happens.
    fixture = _fixture()
    await fixture.start(execution_mode=InvestigationExecutionMode.OPERATOR_LED)
    tasks = InMemoryInvestigationTaskRepository()
    sessions = InMemoryAgentSessionRepository()
    broker_task, session_id, _secret = await _seed_agent_session(fixture, tasks, sessions)
    broker, _uow = _broker(fixture, tasks, sessions)
    session = _verified(session_id, fixture.investigation_id, broker_task)

    first = await broker.invoke(
        session=session,
        tool_name="vt.file_report",
        schema_version="1",
        arguments=_ARGUMENTS,
        client_idempotency_key="sent-before-interrupt",
    )
    replay = await broker.invoke(
        session=session,
        tool_name="vt.file_report",
        schema_version="1",
        arguments=_ARGUMENTS,
        client_idempotency_key="sent-before-interrupt",
    )
    assert replay.invocation_id == first.invocation_id
    assert replay.idempotent_replay is True
    rows = await _invocations(fixture)
    assert len(rows) == 1
    assert rows[0].task_id == broker_task.id
    assert rows[0].agent_session_id == session_id
    assert fixture.tools["vt.file_report"].calls == [_ARGUMENTS]

    tasks._tasks[broker_task.id] = replace(broker_task, lease_fence=broker_task.lease_fence + 1)
    with pytest.raises(AgenticLeaseRefusedError) as refused:
        await broker.invoke(
            session=session,
            tool_name="vt.file_report",
            schema_version="1",
            arguments=_ARGUMENTS,
            client_idempotency_key="after-interrupt",
        )
    assert refused.value.code == "agent_session_lease_stale"
    assert len(await _invocations(fixture)) == 1
    assert fixture.tools["vt.file_report"].calls == [_ARGUMENTS]

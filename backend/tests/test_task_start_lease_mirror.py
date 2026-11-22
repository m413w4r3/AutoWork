"""M3 oracle: an operator ``start`` through the generic route mirrors the lease.

The broker and the session-side lease check read ``agent_sessions.task_lease_*``;
a task started from ``POST /tasks/{id}/commands`` with a signed session must
write that pair in the same command transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import TracebackType
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from cti_app.api.investigation_tasks import _record_session_lease
from cti_app.application.agent_session_verifier import VerifiedAgentSession
from cti_app.application.investigation_persistence import InMemoryAgentSessionRepository
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
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
SESSION_ID = uuid4()
INVESTIGATION_ID = uuid4()
TASK_ID = uuid4()
LEASE_ID = uuid4()


class _Uow:
    def __init__(self, sessions: InMemoryAgentSessionRepository) -> None:
        self.agent_sessions = sessions

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
        return None


def _capabilities() -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable="/usr/local/bin/codex",
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
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "workspace-write"),
            ),
        ),
    )


async def _ready_session() -> tuple[InMemoryAgentSessionRepository, AgentSessionV1]:
    repository = InMemoryAgentSessionRepository()
    capabilities = _capabilities()
    session = AgentSessionV1(
        id=SESSION_ID,
        investigation_id=INVESTIGATION_ID,
        task_id=TASK_ID,
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
        idempotency_key="mirror-session",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{SESSION_ID}",
    )
    stored, _created = await repository.add(session)
    ready = await repository.update_state(
        stored.with_state(AgentSessionState.READY, now=NOW),
        expected_state_version=stored.state_version,
    )
    assert ready is not None
    return repository, ready


def _running_task() -> InvestigationTaskV1:
    return InvestigationTaskV1(
        id=TASK_ID,
        investigation_id=INVESTIGATION_ID,
        task_key="lead-mirror",
        title="Inspect the loader",
        question="Which gate contacts the C2?",
        objective="Identify the downloader.",
        priority=TaskPriority.NORMAL,
        status=TaskStatus.RUNNING,
        status_version=2,
        result_verification_status=ResultVerificationStatus.UNVERIFIED,
        created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
        created_at=NOW,
        updated_at=NOW,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id="codex:operator-1",
        lease_id=LEASE_ID,
        lease_session_id=SESSION_ID,
        lease_fence=1,
        lease_expires_at=NOW + timedelta(seconds=180),
        heartbeat_at=NOW,
    )


def _verified(task_id: UUID = TASK_ID) -> VerifiedAgentSession:
    return VerifiedAgentSession(
        session_id=SESSION_ID,
        investigation_id=INVESTIGATION_ID,
        task_id=task_id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        operator_id="codex:operator-1",
        task_lease_fence=1,
    )


async def test_generic_start_mirrors_the_lease_on_the_session() -> None:
    repository, _ready = await _ready_session()
    uow = _Uow(repository)

    await _record_session_lease(uow, _verified(), _running_task())

    stored = await repository.get(SESSION_ID)
    assert stored is not None
    assert stored.state is AgentSessionState.RUNNING
    assert stored.task_lease_id == LEASE_ID
    assert stored.task_lease_fence == 1

    # A retry is a no-op, not a second state version.
    version = stored.state_version
    await _record_session_lease(uow, _verified(), _running_task())
    again = await repository.get(SESSION_ID)
    assert again is not None and again.state_version == version


async def test_generic_start_refuses_a_session_bound_to_another_task() -> None:
    repository, _ready = await _ready_session()
    with pytest.raises(HTTPException) as exc_info:
        await _record_session_lease(_Uow(repository), _verified(task_id=uuid4()), _running_task())
    assert exc_info.value.status_code == 409
    stored = await repository.get(SESSION_ID)
    assert stored is not None
    assert stored.task_lease_fence is None

"""PostgreSQL concurrency oracles for the M24-05B2 agentic broker (D7/M3).

Real pieces: PostgreSQL repositories, the D7 HMAC verifier, the FastAPI broker
route, the bound ``agent:<session>:<task>:<fence>:<key>`` idempotency key and
the real ``OperatorToolInvocationService`` admission (asyncio plus the
transaction-scoped advisory prefix lock).

Stubbed pieces (no provider, no worker): the invocation service's context
projection and its dispatcher/executor.  The stub persists the journal row
through the same repository the production executor uses, so the single-row
and attribution assertions are real database facts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.api.agentic_tools import router as agentic_tools_router
from cti_app.application.agent_session_verifier import (
    HmacAgentSessionVerifier,
    HmacSessionSecretDeriver,
    build_agent_session_message,
    session_secret_hash,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolResult,
)
from cti_app.application.operator_tool_invocation import OperatorToolInvocationService
from cti_app.config import Settings
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    AgendaItem,
    InvestigationBudget,
    InvestigationExecutionMode,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import (
    InvestigationAgenda,
    InvestigationWorkingState,
)
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
from cti_app.infrastructure.database.session import create_postgres_engine
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

SESSION_KEY = "m" * 32
OPERATOR_ID = "codex:operator-1"
TOOL_NAME = "sample.analyze"
SCHEMA_VERSION = "1"


def _capabilities(now: datetime) -> OperatorCapabilitiesV1:
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
        probed_at=now,
        probes=(
            OperatorProbeCommand(
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "resume", "workspace-write"),
            ),
        ),
    )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    database_url: str,
) -> tuple[UUID, UUID, UUID, bytes]:
    """One investigation, running task, bound session and operator_led state.

    ``agent_sessions.task_id`` and ``investigation_tasks.lease_session_id``
    reference each other, so the task is inserted without a lease, the session
    is inserted against it, then the lease columns are bound by SQL.
    """
    # Lease timestamps are anchored when the test runs, not when pytest
    # collected the module: the signed routes and the repositories compare the
    # lease against the real clock, and a full-suite collection can precede
    # this test by much longer than the 180 s lease TTL.
    now = datetime.now(UTC)
    suffix = uuid4().hex
    session_id = uuid4()
    lease_id = uuid4()
    nonce = b"s" * 32
    secret = HmacSessionSecretDeriver(SESSION_KEY).derive(session_id=session_id, nonce=nonce)
    capabilities = _capabilities(now)
    edition = Edition(
        country=f"Broker fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Broker fixture",
        slug=f"broker-fixture-{suffix}",
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
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        task = InvestigationTaskV1(
            investigation_id=investigation.id,
            task_key=f"lead-{suffix}",
            title="Inspect the loader",
            question="Which gate contacts the C2?",
            objective="Identify the second-stage downloader.",
            priority=TaskPriority.HIGH,
            status=TaskStatus.RUNNING,
            status_version=3,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
            created_at=now - timedelta(minutes=5),
            updated_at=now,
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id=OPERATOR_ID,
            lease_fence=1,
        )
        stored_task = await uow.investigation_tasks.add(task)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        session_row = AgentSessionV1(
            id=session_id,
            investigation_id=investigation.id,
            task_id=stored_task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            state=AgentSessionState.RUNNING,
            state_version=2,
            capability_probe=capabilities.to_payload(),
            capability_sha256=capabilities.capability_sha256,
            probed_at=capabilities.probed_at,
            auth_secret_nonce=nonce,
            auth_secret_hash=session_secret_hash(secret),
            auth_secret_issued_at=now,
            opened_at=now,
            idempotency_key=f"session-{session_id}",
            created_at=now,
            updated_at=now,
            host_session_ref=f"host-{session_id}",
        )
        await uow.agent_sessions.add(session_row)
        working_state = InvestigationWorkingState(
            investigation_id=investigation.id,
            agenda=InvestigationAgenda(
                items=(
                    AgendaItem(question="Which gate contacts the C2?", potential=0.9, rank=1),
                    AgendaItem(
                        question="Which sample carries the loader?",
                        potential=0.7,
                        rank=2,
                    ),
                )
            ),
            execution_mode=InvestigationExecutionMode.OPERATOR_LED,
        )
        await uow.investigation_states.save_if_version(working_state, expected_version=None)
        await uow.commit()
    engine = create_postgres_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE investigation_tasks SET lease_id = :lease_id, "
                    "lease_session_id = :session_id, lease_fence = 1, "
                    "lease_expires_at = :expires_at, heartbeat_at = :heartbeat "
                    "WHERE id = :task_id"
                ),
                {
                    "lease_id": lease_id,
                    "session_id": session_id,
                    "expires_at": now + timedelta(seconds=180),
                    "heartbeat": now,
                    "task_id": stored_task.id,
                },
            )
    finally:
        await engine.dispose()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        bound = await uow.agent_sessions.get(session_id)
        assert bound is not None
        await uow.agent_sessions.update_state(
            bound.with_task_lease(lease_id, 1, now=now), expected_state_version=bound.state_version
        )
        await uow.commit()
    return investigation.id, stored_task.id, session_id, secret


def _registry() -> InMemoryToolRegistry:
    registry = InMemoryToolRegistry()
    registry.register(
        ToolDefinition(
            name=TOOL_NAME,
            schema_version=SCHEMA_VERSION,
            input_schema={
                "type": "object",
                "properties": {
                    "association_id": {"type": "string", "minLength": 36, "maxLength": 36}
                },
                "required": ["association_id"],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Synthetic broker fixture.",),
            capability_key="sample.analysis.read",
        ),
        SimpleNamespace(),  # the stub executor below never dispatches it
    )
    return registry


class _ContextBuilder:
    class article_snapshots:
        @staticmethod
        async def resolve(_synthesis_artifact_id: UUID) -> Any:
            return SimpleNamespace(
                article=SimpleNamespace(claims=()),
                tlp="GREEN",
                do_not_submit=False,
                external_llm_allowed=True,
            )


class _PersistingExecutor:
    """Stub dispatcher that writes the journal through the real repository."""

    _available_capabilities = frozenset({"sample.analysis.read"})

    def __init__(self, uow: Any) -> None:
        self._uow = uow

    def unapproved_budget_for(self, _investigation: Any) -> InvestigationBudget:
        return InvestigationBudget(limits={})

    async def execute_tool_call(
        self,
        *,
        investigation: Any,
        operation_key: str,
        tool_name: str,
        schema_version: str,
        arguments: Any,
        purpose: str,
        actor_id: str | None = None,
        actor_kind: ToolInvocationActorKind | None = None,
        task_id: UUID | None = None,
        agent_session_id: UUID | None = None,
        **_ignored: Any,
    ) -> tuple[ToolInvocation, ToolResult, tuple[UUID, ...], bool]:
        existing = await self._uow.tool_invocations.get_by_operation_key(operation_key)
        if existing is not None:
            return existing, ToolResult(existing.status, output={"summary": "replayed"}), (), False
        invocation = ToolInvocation(
            investigation_id=investigation.id,
            operation_key=operation_key,
            tool_name=tool_name,
            schema_version=schema_version,
            normalized_parameters=arguments,
            purpose=purpose,
            status=ToolInvocationStatus.PENDING,
            task_id=task_id,
            agent_session_id=agent_session_id,
            actor_kind=actor_kind,
            actor_id=actor_id if actor_kind is not None else None,
        )
        stored = await self._uow.tool_invocations.add_if_absent(invocation)
        if stored.status is ToolInvocationStatus.PENDING:
            stored = cast(
                ToolInvocation,
                await self._uow.tool_invocations.update_if_version(
                    stored.transition(ToolInvocationStatus.RUNNING),
                    expected_version=stored.version,
                ),
            )
        # Commit the durable RUNNING row, releasing the transaction-scoped
        # advisory prefix lock exactly like the production executor does.
        await self._uow.commit()
        succeeded = stored.transition(ToolInvocationStatus.SUCCEEDED)
        updated = await self._uow.tool_invocations.update_if_version(
            succeeded, expected_version=stored.version
        )
        await self._uow.commit()
        final = updated or stored
        return final, ToolResult(final.status, output={"summary": "synthetic"}), (), False


class _InvocationService(OperatorToolInvocationService):
    """The real admission logic with a stubbed context projection."""

    async def get_context(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            context=SimpleNamespace(advisor_guidance=None), working_state_version=1
        )


class _CompositionFactory:
    def build(self, uow: Any, investigation_id: UUID, *, operator_mode: bool = False) -> Any:
        del operator_mode
        service = _InvocationService(
            investigation_id=investigation_id,
            investigations=uow.analyst_investigations,
            states=uow.investigation_states,
            envelopes=uow.investigation_envelopes,
            invocations=uow.tool_invocations,
            context_builder=_ContextBuilder(),
            execution=_PersistingExecutor(uow),
            registry=_registry(),
            available_capabilities=frozenset({"sample.analysis.read"}),
            unavailable_tool_reasons={},
            observations=uow.evidence_observations,
            evidence_summaries=None,
        )
        return SimpleNamespace(operator_tool_invocations=service)


def _app(uow_factory: Any, *, verifier_clock: Callable[[], datetime] | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(agentic_tools_router)
    app.state.uow_factory = uow_factory
    app.state.settings = Settings(_env_file=None, m24_agent_session_key=SESSION_KEY)
    app.state.investigation_composition_factory = _CompositionFactory()
    if verifier_clock is not None:
        # Time-boundary oracles pin the D7 verifier clock instead of racing the
        # wall clock; the route honours a mounted verifier over its default.
        app.state.agent_session_verifier = HmacAgentSessionVerifier(
            uow_factory=uow_factory,
            deriver=HmacSessionSecretDeriver(SESSION_KEY),
            clock=verifier_clock,
        )
    return app


def _signed(
    secret: bytes,
    *,
    investigation_id: UUID,
    session_id: UUID,
    idempotency_key: str,
) -> tuple[str, bytes, dict[str, str]]:
    path = f"/api/investigations/{investigation_id}/agent-sessions/{session_id}/tool-calls"
    arguments = {"association_id": str(uuid4())}
    body = json.dumps(
        {"tool_name": TOOL_NAME, "schema_version": SCHEMA_VERSION, "arguments": arguments},
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = str(int(datetime.now(UTC).timestamp()))
    message = build_agent_session_message(
        method="POST",
        path_and_query=path,
        session_id=session_id,
        body=body,
        idempotency_key=idempotency_key,
        timestamp=timestamp,
    )
    headers = {
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
        "X-AutoWork-Agent-Session": str(session_id),
        "X-AutoWork-Agent-Timestamp": timestamp,
        "X-AutoWork-Agent-Signature": "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest(),
    }
    return path, body, headers


@pytest.mark.asyncio
async def test_concurrent_identical_signed_calls_create_one_invocation(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, secret = await _seed(
        session_factory, migrated_postgres_url
    )
    app = _app(uow_factory)
    path, body, headers = _signed(
        secret,
        investigation_id=investigation_id,
        session_id=session_id,
        idempotency_key="mcp-concurrent-key",
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first, second = await asyncio.gather(
            client.post(path, content=body, headers=headers),
            client.post(path, content=body, headers=headers),
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["invocation_id"] == second.json()["invocation_id"]
    assert {first.json()["idempotent_replay"], second.json()["idempotent_replay"]} == {
        False,
        True,
    }

    async with uow_factory() as uow:
        rows = await uow.tool_invocations.list_for_investigation(investigation_id)
    assert len(rows) == 1
    assert rows[0].task_id == task_id
    assert rows[0].agent_session_id == session_id
    assert rows[0].actor_kind is ToolInvocationActorKind.OPERATOR
    assert rows[0].actor_id == OPERATOR_ID


@pytest.mark.asyncio
async def test_expired_lease_race_refuses_before_any_invocation(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, secret = await _seed(
        session_factory, migrated_postgres_url
    )
    now = datetime.now(UTC)
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        # The signature is still valid; the lease expires before the call is
        # admitted, so the D7/broker lease gate must refuse every call.
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE investigation_tasks SET heartbeat_at = :heartbeat, "
                    "lease_expires_at = :expires_at WHERE id = :task_id"
                ),
                {
                    "heartbeat": now - timedelta(seconds=400),
                    "expires_at": now - timedelta(seconds=1),
                    "task_id": task_id,
                },
            )
    finally:
        await engine.dispose()
    app = _app(uow_factory)
    path, body, headers = _signed(
        secret,
        investigation_id=investigation_id,
        session_id=session_id,
        idempotency_key="mcp-expired-lease",
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(path, content=body, headers=headers)

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "agent_session_lease_stale"
    async with uow_factory() as uow:
        assert await uow.tool_invocations.list_for_investigation(investigation_id) == ()


@pytest.mark.asyncio
async def test_lease_deadline_equal_to_the_verifier_clock_is_refused(
    uow_factory: Any, migrated_postgres_url: str
) -> None:
    """The lease boundary is inclusive: at the deadline the call is stale.

    The verifier clock is pinned to the stored deadline, so this is a pure
    boundary oracle and never depends on how long the suite took to get here.
    """
    session_factory = uow_factory.session_factory
    investigation_id, task_id, session_id, secret = await _seed(
        session_factory, migrated_postgres_url
    )
    deadline = datetime.now(UTC)
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE investigation_tasks SET heartbeat_at = :heartbeat, "
                    "lease_expires_at = :expires_at WHERE id = :task_id"
                ),
                {
                    "heartbeat": deadline - timedelta(seconds=60),
                    "expires_at": deadline,
                    "task_id": task_id,
                },
            )
    finally:
        await engine.dispose()
    app = _app(uow_factory, verifier_clock=lambda: deadline)
    path, body, headers = _signed(
        secret,
        investigation_id=investigation_id,
        session_id=session_id,
        idempotency_key="mcp-deadline-lease",
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.post(path, content=body, headers=headers)

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "agent_session_lease_stale"
    async with uow_factory() as uow:
        assert await uow.tool_invocations.list_for_investigation(investigation_id) == ()

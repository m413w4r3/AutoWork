"""Oracles for M24 agent sessions (ADR-0011 D7, D02/D05/D07/B6).

The harness composes the canonical in-memory ports plus a fake
``AgenticOperatorPort``.  It proves the D7 secret derivation and storage, the
single-active-session race, the typed reconciliation outcomes (never a
relaunch), the new-handoff rule after an unresumable loss, the HMAC request
verifier and the attribution pair check.  PostgreSQL-only rules (the partial
unique index, the DB check, the downgrade guard) live in
``tests/integration/test_agent_sessions_postgres.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from cti_app.api.agent_sessions import router as agent_sessions_router
from cti_app.application.agent_session_disclosure import build_session_disclosure_gate
from cti_app.application.agent_session_verification import (
    AgentSessionBindingError,
    AgentSessionLeaseError,
    AgentSessionSignatureError,
    AgentSessionsUnavailableError,
    HmacAgentSessionVerifier,
    HmacSessionSecretDeriver,
    build_agent_session_message,
)
from cti_app.application.agentic_sessions import (
    AgentSessionSecretDeliveryPort,
    PreparedAgentSession,
    SessionConflictError,
    SessionExternalDisclosureDeniedError,
    SessionNetworkIsolationUnavailableError,
    SessionNewHandoffRequiredError,
    SessionResumeUnavailableError,
    SessionSecret,
    SessionSecretDeliveryUnavailableError,
    SessionService,
)
from cti_app.application.identity import Identity, IdentityScope, LocalIdentityProvider
from cti_app.application.investigation_handoffs import HandoffTaskStaleError
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    HandoffRecord,
    InMemoryAgentSessionRepository,
    InMemoryInvestigationHandoffRepository,
    InMemoryInvestigationTaskRepository,
    InvestigationHandoffRepository,
    InvestigationTaskRepository,
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.application.workspace_agent import WorkspaceLocalUnavailableError
from cti_app.config import Settings
from cti_app.domain.agentic_operator import (
    AgentSessionState,
    AgentSessionV1,
    CollectResultsRequest,
    HandoffRequestReceiptV1,
    InstructionDeliveryOutcome,
    InstructionReceiptV1,
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
)
from cti_app.domain.investigation import (
    DisclosureBounds,
    EnvelopeAuthorityKind,
    InvestigationBudget,
    InvestigationEnvelope,
    TLPLevel,
    ToolInvocation,
)
from cti_app.domain.investigation_handoff import (
    HandoffActor,
    HandoffActorKind,
    HandoffTaskSnapshot,
    InvestigationHandoffV1,
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
SESSION_KEY = "0123456789abcdef0123456789abcdef"
EXECUTABLE = "/usr/local/bin/codex"
HUMAN_ACTOR = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
OPERATOR_ACTOR = TaskActor(kind=TaskActorKind.OPERATOR, actor_id="codex:operator-1")
PROBED_MODES = ("exec", "apply", "resume", "workspace-write")
INVESTIGATION_ID = uuid4()


class _InvestigationRepository:
    def __init__(self, investigation_id: UUID) -> None:
        self._investigation = SimpleNamespace(
            id=investigation_id,
            subject_id=uuid4(),
            synthesis_artifact_id=uuid4(),
        )

    async def get(self, investigation_id: UUID) -> Any:
        return self._investigation if investigation_id == self._investigation.id else None


class _EnvelopeRepository:
    """The canonical G2 envelope the disclosure gate reads."""

    def __init__(self) -> None:
        self.items: dict[UUID, Any] = {}

    async def get(self, investigation_id: UUID) -> Any:
        return self.items.get(investigation_id)


class _DecisionRepository:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def append(self, decision: Any) -> None:
        self.items.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(
            decision for decision in self.items if decision.investigation_id == investigation_id
        )


class _LockOrderRepository:
    """Records the advisory/row lock order and the transaction it ran in."""

    def __init__(self, inner: InMemoryInvestigationTaskRepository, harness: _Harness) -> None:
        self._inner = inner
        self._harness = harness

    async def lock_investigation(self, investigation_id: UUID) -> None:
        self._harness.current_locks.append("advisory")

    async def lock_task(self, task_id: UUID) -> InvestigationTaskV1 | None:
        self._harness.current_locks.append("row")
        return await self._inner.lock_task(task_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _FakeUnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self._harness = harness
        self.analyst_investigations: AnalystInvestigationRepository = harness.investigations
        self.analyst_decisions: AnalystDecisionRepository = harness.decisions
        self.investigation_envelopes: Any = harness.envelopes
        self.investigation_tasks: InvestigationTaskRepository = harness.repository
        self.investigation_handoffs: InvestigationHandoffRepository = harness.handoffs
        self.agent_sessions: AgentSessionRepository = harness.sessions

    async def __aenter__(self) -> _FakeUnitOfWork:
        self._harness.transaction_depth += 1
        self._harness.current_locks = []
        return self

    async def __aexit__(self, *_args: object) -> None:
        self._harness.transaction_depth -= 1
        if self._harness.current_locks:
            self._harness.lock_transactions.append(self._harness.current_locks)
        self._harness.current_locks = []

    async def commit(self) -> None:
        return None


def _capabilities(
    *,
    adapter_kind: OperatorAdapterKind = OperatorAdapterKind.CODEX_CLI,
    confinement: OperatorConfinementV1 | None = None,
) -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=adapter_kind,
        executable=EXECUTABLE,
        version="codex-cli 1.2.3",
        modes=("exec", "apply"),
        supports_resume=True,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        permission_modes=("workspace-write",),
        confinement=confinement
        or OperatorConfinementV1(
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
                advertised_modes=PROBED_MODES,
                resolved_version="codex-cli 1.2.3",
            ),
        ),
    )


class _FakeOperator:
    """A deterministic in-memory ``AgenticOperatorPort``; never launches a CLI."""

    adapter_kind = OperatorAdapterKind.CODEX_CLI

    def __init__(self, order: list[str] | None = None) -> None:
        self.capabilities = _capabilities()
        self.order = order if order is not None else []
        self.probed: list[ProbeCapabilitiesRequest] = []
        self.prepared: list[PrepareSessionRequest] = []
        self.started: list[StartSessionRequest] = []
        self.instructions: list[SendInstructionRequest] = []
        self.interrupted: list[InterruptSessionRequest] = []
        self.resumed: list[ResumeSessionRequest] = []
        self.collected: list[CollectResultsRequest] = []
        self.reconciled: list[ReconcileSessionRequest] = []
        self.instruction_outcome = InstructionDeliveryOutcome.ACCEPTED
        self.reconcile_outcome = SessionReconcileOutcome.SESSION_ALIVE
        self.resume_result: ResumeSessionResultV1 | None = None
        self.observer: _Harness | None = None

    def _observe(self, call: str) -> None:
        if self.observer is not None:
            self.observer.host_calls.append((call, self.observer.transaction_depth))

    async def probe_capabilities(self, request: ProbeCapabilitiesRequest) -> OperatorCapabilitiesV1:
        self._observe("probe")
        self.probed.append(request)
        return self.capabilities

    async def prepare_session(self, request: PrepareSessionRequest) -> PreparedOperatorSessionV1:
        self._observe("prepare")
        self.prepared.append(request)
        return PreparedOperatorSessionV1(
            session_id=request.session_id,
            investigation_id=request.investigation_id,
            task_id=request.task_id,
            adapter_kind=request.adapter_kind,
            host_session_ref=f"host-{request.session_id}",
            capabilities=self.capabilities,
            prepared_at=NOW,
        )

    async def start_session(self, request: StartSessionRequest) -> SessionProcessStatusV1:
        self._observe("start")
        self.order.append("start")
        self.started.append(request)
        return SessionProcessStatusV1(
            session_id=request.session_id,
            host_session_ref=request.host_session_ref,
            alive=True,
            last_activity_at=NOW,
            observed_at=NOW,
        )

    async def get_session_status(self, request: SessionStatusRequest) -> SessionProcessStatusV1:
        return SessionProcessStatusV1(
            session_id=request.session_id,
            host_session_ref=request.host_session_ref,
            alive=True,
            observed_at=NOW,
        )

    async def send_instruction(self, request: SendInstructionRequest) -> InstructionReceiptV1:
        self.instructions.append(request)
        return InstructionReceiptV1(
            session_id=request.session_id,
            instruction_id=request.instruction_id,
            outcome=self.instruction_outcome,
            observed_at=NOW,
        )

    async def request_handoff(self, request: RequestHandoffRequest) -> HandoffRequestReceiptV1:
        return HandoffRequestReceiptV1(
            session_id=request.session_id,
            checkpoint_ref="checkpoint-1",
            checkpoint_sha256="c" * 64,
            requested_at=NOW,
        )

    async def interrupt_session(self, request: InterruptSessionRequest) -> InterruptSessionResultV1:
        self._observe("interrupt")
        self.interrupted.append(request)
        return InterruptSessionResultV1(
            session_id=request.session_id, stopped=True, observed_at=NOW
        )

    async def resume_session(self, request: ResumeSessionRequest) -> ResumeSessionResultV1:
        self.resumed.append(request)
        if self.resume_result is not None:
            return self.resume_result
        return ResumeSessionResultV1(
            session_id=request.session_id,
            resumed=True,
            host_session_ref=request.host_session_ref,
            observed_at=NOW,
        )

    async def collect_results(self, request: CollectResultsRequest) -> OperatorResultsV1:
        self.collected.append(request)
        return OperatorResultsV1(
            session_id=request.session_id,
            checkpoint_ref="checkpoint-1",
            checkpoint_sha256="c" * 64,
            observed_at=NOW,
        )

    async def reconcile_session(self, request: ReconcileSessionRequest) -> SessionReconciliationV1:
        self.reconciled.append(request)
        outcome = self.reconcile_outcome
        values: dict[str, Any] = {
            "session_id": request.session_id,
            "outcome": outcome,
            "observed_at": NOW,
            "detail": "fake adapter reconcile",
        }
        if outcome is SessionReconcileOutcome.LOST_WITH_HANDOFF:
            values["checkpoint_ref"] = "checkpoint-1"
            values["checkpoint_sha256"] = "c" * 64
        if outcome is SessionReconcileOutcome.RESUME_AVAILABLE:
            values["host_session_ref"] = request.host_session_ref or "host-resume"
        return SessionReconciliationV1(**values)


class _FakeSecretDelivery:
    """Captures the one-time secret the backend would send over the host socket."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order if order is not None else []
        self.deliveries: list[dict[str, Any]] = []
        self.observer: _Harness | None = None

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
        if self.observer is not None:
            self.observer.host_calls.append(("secret", self.observer.transaction_depth))
        self.order.append("secret")
        self.deliveries.append(
            {
                "session_id": session_id,
                "investigation_id": investigation_id,
                "task_id": task_id,
                "adapter_kind": adapter_kind,
                "host_session_ref": host_session_ref,
                "secret": secret.reveal(),
                "idempotency_key": idempotency_key,
            }
        )

    def secret_for(self, session_id: UUID) -> str:
        matches = [
            delivery["secret"]
            for delivery in self.deliveries
            if delivery["session_id"] == session_id
        ]
        assert matches, f"no secret delivered for session {session_id}"
        assert len(set(matches)) == 1, "the same session must keep one secret"
        return cast(str, matches[-1])


class _ArticleSnapshots:
    """The canonical snapshot facts the disclosure gate reads (no evidence bytes)."""

    def __init__(self, harness: _Harness) -> None:
        self._harness = harness

    async def resolve(self, synthesis_artifact_id: UUID) -> Any:
        return SimpleNamespace(
            tlp=self._harness.snapshot_tlp,
            external_llm_allowed=self._harness.snapshot_external_llm_allowed,
        )


class _Harness:
    def __init__(
        self,
        *,
        key: str | None = SESSION_KEY,
        with_secret_delivery: bool = True,
    ) -> None:
        self.investigation_id = INVESTIGATION_ID
        self.tasks = InMemoryInvestigationTaskRepository()
        self.handoffs = InMemoryInvestigationHandoffRepository()
        self.sessions = InMemoryAgentSessionRepository()
        self.investigations = _InvestigationRepository(self.investigation_id)
        self.decisions = _DecisionRepository()
        self.envelopes = _EnvelopeRepository()
        self.snapshot_tlp = "clear"
        self.snapshot_external_llm_allowed = True
        self.lock_transactions: list[list[str]] = []
        self.current_locks: list[str] = []
        self.host_calls: list[tuple[str, int]] = []
        self.transaction_depth = 0
        self.repository = _LockOrderRepository(self.tasks, self)
        self.uow = _FakeUnitOfWork(self)
        self.order: list[str] = []
        self.operator = _FakeOperator(self.order)
        self.operator.observer = self
        self.deriver = HmacSessionSecretDeriver(key) if key is not None else None
        self.secret_delivery: AgentSessionSecretDeliveryPort | None = (
            _FakeSecretDelivery(self.order) if with_secret_delivery else None
        )
        if isinstance(self.secret_delivery, _FakeSecretDelivery):
            self.secret_delivery.observer = self
        self.disclosure = build_session_disclosure_gate(
            uow_factory=lambda: self.uow,
            article_snapshots=_ArticleSnapshots(self),
            settings=Settings(advisor_disclosure_profile="standard"),
        )
        self.seed_envelope()
        self.service = SessionService(
            uow_factory=lambda: self.uow,
            deriver=self.deriver,
            operator=self.operator,
            secret_delivery=self.secret_delivery,
            disclosure=self.disclosure,
            cli_network_mode="isolated_egress",
            clock=lambda: NOW,
        )
        self.fake_delivery = self.secret_delivery

    def seed_envelope(
        self,
        *,
        tlp: TLPLevel | None = TLPLevel.CLEAR,
        external_llm_allowed: bool = True,
        authority_kind: EnvelopeAuthorityKind = EnvelopeAuthorityKind.HUMAN_G2,
    ) -> InvestigationEnvelope:
        envelope = InvestigationEnvelope(
            investigation_id=self.investigation_id,
            allowed_providers=("bridge",),
            budget=InvestigationBudget(limits={}),
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=tlp,
                do_not_submit=False,
                external_llm_allowed=external_llm_allowed,
            ),
            approval_decision_id=uuid4(),
            approval_actor_id="analyst:test",
            authority_kind=authority_kind,
        )
        self.envelopes.items[self.investigation_id] = envelope
        return envelope

    async def seed_task(
        self,
        *,
        status: TaskStatus = TaskStatus.ASSIGNED,
        status_version: int = 2,
        task_id: UUID | None = None,
    ) -> InvestigationTaskV1:
        task = InvestigationTaskV1(
            id=task_id or uuid4(),
            investigation_id=self.investigation_id,
            task_key="lead-loader",
            title="Inspect the loader",
            question="Which network gate does the loader use?",
            objective="Identify the first-stage C2 gate.",
            priority=TaskPriority.HIGH,
            status=status,
            status_version=status_version,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
            created_at=NOW,
            updated_at=NOW,
            assigned_actor_kind=TaskActorKind.OPERATOR,
            assigned_actor_id=OPERATOR_ACTOR.actor_id,
        )
        return await self.tasks.add(task)

    async def add_handoff(
        self,
        task: InvestigationTaskV1,
        *,
        version: int = 1,
        handoff_id: UUID | None = None,
    ) -> InvestigationHandoffV1:
        handoff = InvestigationHandoffV1(
            handoff_id=str(handoff_id or uuid4()),
            investigation_id=str(task.investigation_id),
            task_id=str(task.id),
            version=version,
            created_at=NOW,
            from_actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
            to_actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=OPERATOR_ACTOR.actor_id),
            task=HandoffTaskSnapshot(
                status=task.status,
                status_version=task.status_version,
                question=task.question,
                objective=task.objective,
                assigned_actor_kind=HandoffActorKind.OPERATOR,
                assigned_actor_id=task.assigned_actor_id,
            ),
            next_action="Continue the analysis.",
        )
        record = HandoffRecord(
            handoff=handoff,
            idempotency_key=f"handoff-{handoff.handoff_id}",
            request_sha256="d" * 64,
            created_by=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
        )
        stored, _created = await self.handoffs.append_if_absent(record)
        return stored.handoff

    async def open(
        self,
        *,
        handoff: InvestigationHandoffV1 | None = None,
        task: InvestigationTaskV1 | None = None,
        idempotency_key: str = "open-session-1",
        executable: str = EXECUTABLE,
    ) -> PreparedAgentSession:
        assert task is not None, "seed and pass the task"
        bound = handoff or await self.add_handoff(task)
        return await self.service.open_session(
            self.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(bound.handoff_id),
            executable=executable,
            actor=HUMAN_ACTOR,
            idempotency_key=idempotency_key,
        )

    def expire_lease(self, task_id: UUID) -> None:
        task = self.tasks._tasks[task_id]
        self.tasks._tasks[task_id] = replace(
            task,
            heartbeat_at=NOW - timedelta(seconds=200),
            lease_expires_at=NOW - timedelta(seconds=1),
        )

    def task_row(self, task_id: UUID) -> InvestigationTaskV1:
        return self.tasks._tasks[task_id]


def _session_for_lost(
    harness: _Harness,
    task: InvestigationTaskV1,
    handoff_id: UUID,
    *,
    outcome: SessionReconcileOutcome,
) -> AgentSessionV1:
    """A previously lost session with a typed reconciliation outcome."""
    session_id = uuid4()
    nonce = b"n" * 32
    assert harness.deriver is not None
    secret = harness.deriver.derive(session_id=session_id, nonce=nonce)
    capabilities = harness.operator.capabilities
    reconciliation = SessionReconciliationV1(
        session_id=session_id,
        outcome=outcome,
        observed_at=NOW,
        host_session_ref=f"host-{session_id}",
        detail="previous loss",
    )
    return AgentSessionV1(
        id=session_id,
        investigation_id=harness.investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.LOST,
        state_version=4,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=nonce,
        auth_secret_hash=hashlib.sha256(secret).hexdigest(),
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key="previous-session",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
        handoff_id=handoff_id,
        close_reason=outcome.value,
        reconciliation=reconciliation.payload(),
        reconciliation_sha256=reconciliation.reconciliation_sha256,
    )


def _ready_session(
    harness: _Harness,
    task: InvestigationTaskV1,
    handoff_id: UUID,
    *,
    session_id: UUID | None = None,
) -> AgentSessionV1:
    """A row a crash left ``ready`` without a lease (open_session phase 2/3)."""
    session_id = session_id or uuid4()
    nonce = b"r" * 32
    capabilities = harness.operator.capabilities
    return AgentSessionV1(
        id=session_id,
        investigation_id=harness.investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.READY,
        state_version=2,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=nonce,
        auth_secret_hash=hashlib.sha256(nonce).hexdigest(),
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"crashed-{session_id}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
        handoff_id=handoff_id,
    )


def _signed(
    secret_hex: str,
    *,
    method: str,
    path_and_query: str,
    session_id: UUID,
    body: bytes = b"",
    idempotency_key: str | None = None,
    timestamp: datetime = NOW,
) -> tuple[str, str]:
    timestamp_text = str(int(timestamp.timestamp()))
    message = build_agent_session_message(
        method=method,
        path_and_query=path_and_query,
        session_id=session_id,
        body=body,
        idempotency_key=idempotency_key,
        timestamp=timestamp_text,
    )
    signature = hmac.new(bytes.fromhex(secret_hex), message, hashlib.sha256).hexdigest()
    return timestamp_text, f"v1={signature}"


# --------------------------------------------------------------------- secret


async def test_session_hmac_secret_issued_at_prepare_and_hash_stored() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)

    prepared = await harness.service.prepare(
        harness.investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        handoff_id=UUID(handoff.handoff_id),
        executable=EXECUTABLE,
        actor=HUMAN_ACTOR,
        idempotency_key="prepare-1",
    )
    secret = prepared.secret.reveal()
    assert len(secret) == 64
    assert prepared.session.state is AgentSessionState.READY

    stored = await harness.sessions.get(prepared.session.id)
    assert stored is not None
    assert len(stored.auth_secret_nonce) == 32
    assert stored.auth_secret_hash == hashlib.sha256(bytes.fromhex(secret)).hexdigest()
    expected = hmac.new(
        SESSION_KEY.encode("utf-8"),
        b"autowork-agent-session.v1\0"
        + str(prepared.session.id).encode("ascii")
        + b"\0"
        + stored.auth_secret_nonce,
        hashlib.sha256,
    ).digest()
    assert expected.hex() == secret
    assert len(harness.operator.prepared) == 1
    delivery = harness.fake_delivery
    assert delivery is not None
    assert delivery.secret_for(prepared.session.id) == secret
    assert delivery.deliveries[-1]["host_session_ref"] == f"host-{prepared.session.id}"
    assert delivery.deliveries[-1]["idempotency_key"] == "prepare-1"


async def test_prepare_replay_rederives_same_secret_without_reprobing() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    first = await harness.service.prepare(
        harness.investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        handoff_id=UUID(handoff.handoff_id),
        executable=EXECUTABLE,
        actor=HUMAN_ACTOR,
        idempotency_key="prepare-replay",
    )
    second = await harness.service.prepare(
        harness.investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        handoff_id=UUID(handoff.handoff_id),
        executable=EXECUTABLE,
        actor=HUMAN_ACTOR,
        idempotency_key="prepare-replay",
    )
    assert second.session.id == first.session.id
    assert second.secret.reveal() == first.secret.reveal()
    assert len(harness.operator.probed) == 1
    assert len(harness.operator.prepared) == 1


async def test_open_session_delivers_the_secret_before_the_adapter_start() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    await harness.open(task=task)
    # SESSION-RUNNER §6: the host prepare IS the one-shot secret hand-off, so
    # start_session may only follow a successful delivery.
    assert harness.order == ["secret", "start"]


async def test_session_locks_are_advisory_then_row_and_host_calls_run_outside() -> None:
    """B3: no row lock before the investigation advisory, no host call under one."""
    harness = _Harness()
    task = await harness.seed_task()
    await harness.open(task=task)

    assert harness.lock_transactions, "the session start must use the task locks"
    # B3: in every transaction that locks the task row, the investigation
    # advisory lock is already held.  The event append may re-lock the row in
    # the same transaction, so row locks are not one-per-advisory.
    for transaction in harness.lock_transactions:
        if "row" in transaction:
            assert transaction[0] == "advisory", transaction
    assert sum(t.count("advisory") for t in harness.lock_transactions) >= 3
    # probe, host prepare, secret delivery and host start all ran between
    # transactions (depth 0); a nested call would record 1 or more.
    assert [name for name, _depth in harness.host_calls] == [
        "probe",
        "prepare",
        "secret",
        "start",
    ]
    assert all(depth == 0 for _name, depth in harness.host_calls)


async def test_session_start_validates_under_lock_then_starts_the_host_unlocked() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    prepared = await harness.service.prepare(
        harness.investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        handoff_id=UUID(handoff.handoff_id),
        executable=EXECUTABLE,
        actor=HUMAN_ACTOR,
        idempotency_key="prepare-then-start",
    )
    harness.host_calls.clear()
    harness.lock_transactions.clear()
    await harness.service.start(harness.investigation_id, prepared.session.id, actor=HUMAN_ACTOR)
    assert [name for name, _depth in harness.host_calls] == ["start"]
    assert harness.host_calls[0][1] == 0
    locked = [tx for tx in harness.lock_transactions if tx]
    assert locked[0][0] == "advisory"
    for transaction in locked:
        if "row" in transaction:
            assert transaction[0] == "advisory", transaction


async def test_secret_never_appears_in_repr_logs_or_payloads() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    prepared = await harness.open(task=task)
    secret = prepared.secret.reveal()

    assert secret not in repr(prepared)
    assert secret not in str(prepared)
    assert secret not in repr(prepared.secret)
    assert secret not in str(prepared.secret)
    assert repr(prepared.secret) == "SessionSecret(***)"
    log_fields = prepared.log_fields()
    assert log_fields["secret"] == "***"
    assert secret not in repr(log_fields)
    payload = prepared.session.to_payload()
    assert "auth_secret_nonce" not in payload
    assert "auth_secret_hash" not in payload
    assert secret not in repr(payload)
    with pytest.raises(ValueError):
        SessionSecret("not-a-secret")


def test_server_key_must_be_exactly_32_bytes() -> None:
    with pytest.raises(ValidationError, match="exactly 32 bytes"):
        Settings(m24_agent_session_key="too-short")
    valid = Settings(m24_agent_session_key=SESSION_KEY)
    assert valid.m24_agent_session_key is not None
    assert valid.m24_agent_session_key.get_secret_value() == SESSION_KEY


async def test_missing_server_key_fails_closed() -> None:
    harness = _Harness(key=None)
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    with pytest.raises(AgentSessionsUnavailableError) as exc_info:
        await harness.service.prepare(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(handoff.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="prepare-no-key",
        )
    assert exc_info.value.code == "agent_sessions_unavailable"
    assert exc_info.value.status_code == 503
    assert await harness.sessions.list_for_task(task.id) == ()


async def test_secret_delivery_absent_fails_closed() -> None:
    harness = _Harness(with_secret_delivery=False)
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    with pytest.raises(SessionSecretDeliveryUnavailableError) as exc_info:
        await harness.service.prepare(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(handoff.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="prepare-no-host",
        )
    assert exc_info.value.code == "agent_session_secret_delivery_unavailable"
    assert exc_info.value.status_code == 503


# --------------------------------------------------------------- concurrency


async def test_concurrent_session_start_single_active() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)

    first, second = await asyncio.gather(
        harness.open(task=task, handoff=handoff, idempotency_key="race-a"),
        harness.open(task=task, handoff=handoff, idempotency_key="race-b"),
        return_exceptions=True,
    )
    winners = [item for item in (first, second) if isinstance(item, PreparedAgentSession)]
    losers = [item for item in (first, second) if isinstance(item, Exception)]
    assert len(winners) == 1
    assert len(losers) == 1
    # With the task row locked, the second open sees the started task and the
    # D5 start gate refuses with handoff_task_stale; the active-session index is
    # the last-resort guard exercised by the PostgreSQL race test.
    assert isinstance(losers[0], (SessionConflictError, HandoffTaskStaleError))
    assert losers[0].code in {"active_session_exists", "handoff_task_stale"}
    assert len(harness.operator.started) == 1
    sessions = await harness.sessions.list_for_task(task.id)
    assert len(sessions) == 1
    assert sessions[0].state is AgentSessionState.RUNNING
    running = harness.task_row(task.id)
    assert running.status is TaskStatus.RUNNING


async def test_same_idempotency_key_replays_the_won_session() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    first, second = await asyncio.gather(
        harness.open(task=task, handoff=handoff, idempotency_key="same-open"),
        harness.open(task=task, handoff=handoff, idempotency_key="same-open"),
    )
    assert first.session.id == second.session.id
    assert first.secret.reveal() == second.secret.reveal()
    assert len(harness.operator.started) == 1
    assert len(await harness.sessions.list_for_task(task.id)) == 1


# ------------------------------------------------------------- reconciliation


@pytest.mark.parametrize("outcome", list(SessionReconcileOutcome))
async def test_reconcile_outcomes_never_auto_relaunch(
    outcome: SessionReconcileOutcome,
) -> None:
    harness = _Harness()
    task = await harness.seed_task()
    prepared = await harness.open(task=task)
    before_task = harness.task_row(task.id)
    before_start = len(harness.operator.started)
    before_resume = len(harness.operator.resumed)
    harness.operator.reconcile_outcome = outcome

    reconciled = await harness.service.reconcile(
        harness.investigation_id, prepared.session.id, actor=OPERATOR_ACTOR
    )

    expected_state = {
        SessionReconcileOutcome.SESSION_ALIVE: AgentSessionState.RUNNING,
        SessionReconcileOutcome.TERMINATED_CLEAN: AgentSessionState.CLOSED,
        SessionReconcileOutcome.LOST_WITH_HANDOFF: AgentSessionState.LOST,
        SessionReconcileOutcome.LOST_UNRECONCILED: AgentSessionState.LOST,
        SessionReconcileOutcome.RESUME_AVAILABLE: AgentSessionState.LOST,
        SessionReconcileOutcome.RESUME_UNAVAILABLE: AgentSessionState.LOST,
    }[outcome]
    assert reconciled.state is expected_state
    assert reconciled.reconciliation is not None
    assert reconciled.reconciliation_sha256 is not None
    assert reconciled.reconciliation["outcome"] == outcome.value
    if outcome is SessionReconcileOutcome.LOST_WITH_HANDOFF:
        assert reconciled.checkpoint_ref == "checkpoint-1"
        assert reconciled.checkpoint_sha256 == "c" * 64
    if expected_state is AgentSessionState.CLOSED:
        assert reconciled.closed_at is not None
    else:
        assert reconciled.closed_at is None

    # No relaunch: no second session, no adapter start/resume, no task/lease write.
    assert len(await harness.sessions.list_for_task(task.id)) == 1
    assert len(harness.operator.started) == before_start
    assert len(harness.operator.resumed) == before_resume
    after_task = harness.task_row(task.id)
    assert (
        after_task.status,
        after_task.status_version,
        after_task.lease_fence,
        after_task.lease_session_id,
    ) == (
        before_task.status,
        before_task.status_version,
        before_task.lease_fence,
        before_task.lease_session_id,
    )


async def test_resume_unavailable_requires_new_session_and_handoff() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    lost = _session_for_lost(
        harness,
        task,
        UUID(handoff.handoff_id),
        outcome=SessionReconcileOutcome.RESUME_UNAVAILABLE,
    )
    stored, created = await harness.sessions.add(lost)
    assert created

    # The human resume decision is refused: the adapter never proved a resume.
    with pytest.raises(SessionResumeUnavailableError) as exc_info:
        await harness.service.resume(harness.investigation_id, stored.id, actor=HUMAN_ACTOR)
    assert exc_info.value.code == "resume_unavailable"
    unchanged = await harness.sessions.get(stored.id)
    assert unchanged is not None and unchanged.state is AgentSessionState.LOST

    # A new session cannot reuse the handoff of the unresumable loss.
    with pytest.raises(SessionNewHandoffRequiredError) as new_handoff:
        await harness.service.prepare(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(handoff.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="retry-after-loss",
        )
    assert new_handoff.value.code == "new_handoff_required"
    assert len(await harness.sessions.list_for_task(task.id)) == 1

    # A new handoff (the task snapshot is unchanged) allows a new session.
    fresh_handoff = await harness.add_handoff(task, version=2)
    prepared = await harness.service.prepare(
        harness.investigation_id,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        handoff_id=UUID(fresh_handoff.handoff_id),
        executable=EXECUTABLE,
        actor=HUMAN_ACTOR,
        idempotency_key="new-session-after-loss",
    )
    assert prepared.session.id != stored.id
    assert prepared.session.state is AgentSessionState.READY
    assert len(await harness.sessions.list_for_task(task.id)) == 2


async def test_resume_refusal_closes_a_stopped_session_lost() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    prepared = await harness.open(task=task)
    stopped = await harness.service.interrupt(
        harness.investigation_id,
        prepared.session.id,
        reason="operator asked to stop",
        actor=HUMAN_ACTOR,
    )
    assert stopped.state is AgentSessionState.STOPPED
    harness.operator.resume_result = ResumeSessionResultV1(
        session_id=prepared.session.id,
        resumed=False,
        detail="process checkpoint is gone",
        observed_at=NOW,
    )
    with pytest.raises(SessionResumeUnavailableError):
        await harness.service.resume(
            harness.investigation_id, prepared.session.id, actor=HUMAN_ACTOR
        )
    lost = await harness.sessions.get(prepared.session.id)
    assert lost is not None
    assert lost.state is AgentSessionState.LOST
    assert lost.close_reason == "resume_unavailable"


async def test_stale_handoff_blocks_session_start() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    stale = await harness.add_handoff(task)
    # The task moves after the handoff was written.
    harness.tasks._tasks[task.id] = replace(
        harness.tasks._tasks[task.id], status_version=task.status_version + 1
    )
    with pytest.raises(HandoffTaskStaleError) as exc_info:
        await harness.service.open_session(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(stale.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="stale-open",
        )
    assert exc_info.value.code == "handoff_task_stale"
    assert await harness.sessions.list_for_task(task.id) == ()


# ------------------------------------------------------------------ verifier


async def _running_harness() -> tuple[_Harness, PreparedAgentSession]:
    harness = _Harness()
    task = await harness.seed_task()
    prepared = await harness.open(task=task)
    return harness, prepared


def _verifier(harness: _Harness) -> HmacAgentSessionVerifier:
    return HmacAgentSessionVerifier(
        uow_factory=lambda: harness.uow,
        deriver=harness.deriver,
        clock=lambda: NOW,
    )


async def _verify(
    harness: _Harness,
    prepared: PreparedAgentSession,
    *,
    investigation_id: UUID | None = None,
    session_id: UUID | None = None,
    body: bytes = b"",
    timestamp: datetime = NOW,
    require_current_lease: bool = False,
    signed_body: bytes | None = None,
    signed_session_id: UUID | None = None,
) -> Any:
    target = session_id or prepared.session.id
    signed_for = signed_session_id or prepared.session.id
    timestamp_text, signature = _signed(
        prepared.secret.reveal(),
        method="POST",
        path_and_query=f"/api/investigations/{harness.investigation_id}/agent-sessions/{target}/collect",
        session_id=signed_for,
        body=signed_body if signed_body is not None else body,
        timestamp=timestamp,
    )
    return await _verifier(harness).verify_session_request(
        method="POST",
        path_and_query=f"/api/investigations/{harness.investigation_id}/agent-sessions/{target}/collect",
        session_id=target,
        timestamp=timestamp_text,
        signature=signature,
        body=body,
        idempotency_key=None,
        investigation_id=investigation_id or harness.investigation_id,
        require_current_lease=require_current_lease,
        now=NOW,
    )


async def test_signature_accepts_the_owning_session() -> None:
    harness, prepared = await _running_harness()
    verified = await _verify(harness, prepared)
    assert verified.session_id == prepared.session.id
    assert verified.investigation_id == harness.investigation_id
    assert verified.task_id == prepared.session.task_id


async def test_signature_rejects_bad_timestamp() -> None:
    harness, prepared = await _running_harness()
    with pytest.raises(AgentSessionSignatureError):
        await _verify(harness, prepared, timestamp=NOW - timedelta(seconds=301))


async def test_signature_rejects_a_replayed_body_change() -> None:
    harness, prepared = await _running_harness()
    with pytest.raises(AgentSessionSignatureError):
        await _verify(harness, prepared, body=b'{"cursor":"changed"}', signed_body=b"{}")


async def test_signature_rejects_wrong_session() -> None:
    harness, prepared = await _running_harness()
    with pytest.raises(AgentSessionSignatureError):
        await _verify(harness, prepared, session_id=uuid4(), signed_session_id=prepared.session.id)


async def test_signature_rejects_other_investigation() -> None:
    harness, prepared = await _running_harness()
    with pytest.raises(AgentSessionBindingError):
        await _verify(harness, prepared, investigation_id=uuid4())


async def test_signature_requires_current_lease_for_writes() -> None:
    harness, prepared = await _running_harness()
    await _verify(harness, prepared, require_current_lease=True)
    harness.expire_lease(prepared.session.task_id)
    with pytest.raises(AgentSessionLeaseError):
        await _verify(harness, prepared, require_current_lease=True)


async def test_verifier_without_key_fails_closed() -> None:
    harness, prepared = await _running_harness()
    verifier = HmacAgentSessionVerifier(
        uow_factory=lambda: harness.uow, deriver=None, clock=lambda: NOW
    )
    with pytest.raises(AgentSessionsUnavailableError):
        await verifier.verify_session_request(
            method="POST",
            path_and_query="/api/investigations/x/agent-sessions/y/collect",
            session_id=prepared.session.id,
            timestamp=str(int(NOW.timestamp())),
            signature="v1=" + "0" * 64,
            body=b"",
            idempotency_key=None,
            investigation_id=harness.investigation_id,
            now=NOW,
        )


# --------------------------------------------------------------- instructions


async def test_instruction_is_queued_while_agent_busy_and_acked_signed() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    prepared = await harness.open(task=task)

    queued = await harness.service.enqueue_instruction(
        harness.investigation_id,
        prepared.session.id,
        text="Check the second-stage gate.",
        actor=HUMAN_ACTOR,
        idempotency_key="instruction-1",
    )
    assert queued.state.value == "queued"
    assert harness.operator.instructions == []
    replayed = await harness.service.enqueue_instruction(
        harness.investigation_id,
        prepared.session.id,
        text="Check the second-stage gate.",
        actor=HUMAN_ACTOR,
        idempotency_key="instruction-1",
    )
    assert replayed.id == queued.id

    acknowledged = await harness.service.acknowledge_instructions(
        harness.investigation_id,
        prepared.session.id,
        instruction_ids=(queued.id,),
        actor=OPERATOR_ACTOR,
    )
    assert acknowledged[0].state.value == "acknowledged"
    assert acknowledged[0].delivered_at is not None
    assert acknowledged[0].acknowledged_at is not None


# --------------------------------------------------------------- attribution


def test_tool_invocation_attribution_pair_check() -> None:
    common: dict[str, Any] = {
        "investigation_id": uuid4(),
        "operation_key": "op-" + uuid4().hex,
        "tool_name": "advisor.consult",
        "schema_version": "advisor-consult.v1",
        "normalized_parameters": {},
        "purpose": "test",
    }
    with pytest.raises(ValueError, match="task_id and agent_session_id"):
        ToolInvocation(**common, task_id=uuid4())
    with pytest.raises(ValueError, match="task_id and agent_session_id"):
        ToolInvocation(**common, agent_session_id=uuid4())
    attributed = ToolInvocation(**common, task_id=uuid4(), agent_session_id=uuid4())
    assert attributed.agent_session_id is not None
    unlinked = ToolInvocation(**common)
    assert unlinked.task_id is None and unlinked.agent_session_id is None


# --------------------------------------------------------------------- routes


class _OperatorClientIdentity:
    async def current(self) -> Identity:
        return Identity(
            actor_id="operator:operator-client",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


class _OsintPolicyService:
    async def effective_for_investigation(self, investigation_id: UUID) -> Any:
        return None


class _CompositionFactory:
    """The minimal canonical composition the disclosure gate and probe need."""

    def __init__(self, harness: _Harness) -> None:
        self._harness = harness

    @property
    def article_snapshots(self) -> _ArticleSnapshots:
        return _ArticleSnapshots(self._harness)

    @property
    def osint_open_policy_service(self) -> _OsintPolicyService:
        return _OsintPolicyService()


def _api_app(harness: _Harness, provider: Any) -> FastAPI:
    application = FastAPI()
    application.include_router(agent_sessions_router)
    application.state.uow_factory = lambda: harness.uow
    # The harness drives a local-identity "human"; the production guard that refuses agent
    # sessions under the local identity mode has its own oracle (test_agent_identity_boundary).
    application.state.settings = Settings(
        m24_agent_session_key=SESSION_KEY, agent_sessions_allow_local_identity=True
    )
    application.state.agentic_operator_port = harness.operator
    application.state.agent_session_secret_delivery = harness.secret_delivery
    application.state.identity_provider = provider
    application.state.investigation_composition_factory = _CompositionFactory(harness)
    return application


async def test_agent_session_routes_require_human_and_operator_signature() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)

    human_app = _api_app(harness, LocalIdentityProvider("dev-analyst"))
    async with AsyncClient(
        transport=ASGITransport(app=human_app), base_url="http://test"
    ) as client:
        opened = await client.post(
            f"/api/investigations/{harness.investigation_id}/agent-sessions",
            headers={"Idempotency-Key": "api-open"},
            json={
                "task_id": str(task.id),
                "adapter_kind": "codex_cli",
                "handoff_id": handoff.handoff_id,
                "executable": EXECUTABLE,
            },
        )
        assert opened.status_code == 200, opened.text
        assert "secret" not in opened.json()
        session_id = opened.json()["session"]["id"]
        assert opened.json()["session"]["state"] == "running"
        delivery = harness.fake_delivery
        assert delivery is not None
        secret = delivery.secret_for(UUID(session_id))
        assert len(secret) == 64
        assert secret not in opened.text
        assert secret not in repr(opened.json())

        fetched = await client.get(
            f"/api/investigations/{harness.investigation_id}/agent-sessions/{session_id}"
        )
        assert fetched.status_code == 200
        assert "secret" not in fetched.json()
        assert "auth_secret_nonce" not in fetched.json()

        listed = await client.get(f"/api/investigations/{harness.investigation_id}/agent-sessions")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [session_id]

        # A signed operator collects results from its own running session.
        path = f"/api/investigations/{harness.investigation_id}/agent-sessions/{session_id}/collect"
        body = b"{}"
        timestamp_text, signature = _signed(
            secret,
            method="POST",
            path_and_query=path,
            session_id=UUID(session_id),
            body=body,
            timestamp=datetime.now(UTC),
        )
        signed_headers = {
            "x-autowork-agent-session": session_id,
            "x-autowork-agent-timestamp": timestamp_text,
            "x-autowork-agent-signature": signature,
        }
        collected = await client.post(path, headers=signed_headers, content=body)
        assert collected.status_code == 200, collected.text
        assert collected.json()["checkpoint_sha256"] == "c" * 64

        interrupted = await client.post(
            f"/api/investigations/{harness.investigation_id}/agent-sessions/{session_id}/interrupt",
            json={"reason": "human stop"},
        )
        assert interrupted.status_code == 200
        assert interrupted.json()["state"] == "stopped"

    operator_client_app = _api_app(harness, _OperatorClientIdentity())
    async with AsyncClient(
        transport=ASGITransport(app=operator_client_app), base_url="http://test"
    ) as client:
        refused = await client.post(
            f"/api/investigations/{harness.investigation_id}/agent-sessions",
            headers={"Idempotency-Key": "api-open-refused"},
            json={
                "task_id": str(task.id),
                "adapter_kind": "codex_cli",
                "handoff_id": handoff.handoff_id,
                "executable": EXECUTABLE,
            },
        )
        assert refused.status_code == 403
        assert refused.json()["detail"]["code"] == "verified_human_operator_required"


async def test_signed_route_rejects_a_bad_signature() -> None:
    harness, prepared = await _running_harness()
    app = _api_app(harness, _OperatorClientIdentity())
    path = (
        f"/api/investigations/{harness.investigation_id}"
        f"/agent-sessions/{prepared.session.id}/collect"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            path,
            headers={
                "x-autowork-agent-session": str(prepared.session.id),
                "x-autowork-agent-timestamp": str(int(datetime.now(UTC).timestamp())),
                "x-autowork-agent-signature": "v1=" + "0" * 64,
            },
            content=b"{}",
        )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "agent_session_signature_invalid"


# --------------------------------------------------- disclosure gate (R03 B2)


async def test_open_refused_when_external_llm_disallowed_without_host_call() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    harness.seed_envelope(external_llm_allowed=False)

    with pytest.raises(SessionExternalDisclosureDeniedError) as exc_info:
        await harness.service.open_session(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(handoff.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="open-disclosure-denied",
        )
    assert exc_info.value.code == "agent_session_external_llm_not_allowed"
    assert exc_info.value.status_code == 403
    assert exc_info.value.details == {"reason": "external_llm_not_allowed"}
    # No host probe, no host prepare/start, no session row and no secret.
    assert harness.operator.probed == []
    assert harness.operator.prepared == []
    assert harness.operator.started == []
    assert await harness.sessions.list_for_task(task.id) == ()
    assert harness.fake_delivery is not None
    assert harness.fake_delivery.deliveries == []


async def test_open_refused_when_tlp_is_red() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    harness.seed_envelope(tlp=TLPLevel.RED)

    with pytest.raises(SessionExternalDisclosureDeniedError) as exc_info:
        await harness.service.open_session(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(handoff.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="open-tlp-red",
        )
    assert exc_info.value.details == {"reason": "external_llm_not_allowed"}
    assert harness.operator.probed == []
    assert harness.operator.prepared == []
    assert await harness.sessions.list_for_task(task.id) == ()


async def test_open_refused_when_envelope_is_absent() -> None:
    harness = _Harness()
    harness.envelopes.items.clear()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)

    with pytest.raises(SessionExternalDisclosureDeniedError) as exc_info:
        await harness.service.open_session(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(handoff.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="open-no-envelope",
        )
    assert exc_info.value.details == {"reason": "envelope_absent"}
    assert harness.operator.probed == []
    assert await harness.sessions.list_for_task(task.id) == ()


async def test_allowed_envelope_still_opens_the_session() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    prepared = await harness.open(task=task)
    assert prepared.session.state is AgentSessionState.RUNNING
    assert len(harness.operator.started) == 1


async def test_live_instruction_delivery_is_regated_by_the_disclosure_gate() -> None:
    """R03b M1: a revoke after the opening stops the next host delivery."""
    harness = _Harness()
    task = await harness.seed_task()
    prepared = await harness.open(task=task)
    stored = await harness.sessions.get(prepared.session.id)
    assert stored is not None
    idle = await harness.sessions.update_state(
        stored.with_state(AgentSessionState.AWAITING_INSTRUCTION, now=NOW),
        expected_state_version=stored.state_version,
    )
    assert idle is not None
    harness.seed_envelope(external_llm_allowed=False)

    with pytest.raises(SessionExternalDisclosureDeniedError) as exc_info:
        await harness.service.enqueue_instruction(
            harness.investigation_id,
            prepared.session.id,
            text="This must not reach the provider.",
            actor=HUMAN_ACTOR,
            idempotency_key="instruction-after-revoke",
        )
    assert exc_info.value.details == {"reason": "external_llm_not_allowed"}
    assert harness.operator.instructions == []
    assert await harness.sessions.list_instructions(prepared.session.id) == ()


async def test_start_session_exception_marks_the_session_failed_and_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLOCKER 1(a): an adapter exception is never a silent orphan."""
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)

    async def explode(_request: StartSessionRequest) -> SessionProcessStatusV1:
        raise WorkspaceLocalUnavailableError("host start failed")

    monkeypatch.setattr(harness.operator, "start_session", explode)
    with pytest.raises(WorkspaceLocalUnavailableError):
        await harness.open(handoff=handoff, task=task, idempotency_key="start-explodes")

    sessions = await harness.sessions.list_for_task(task.id)
    assert len(sessions) == 1
    assert sessions[0].state is AgentSessionState.FAILED
    assert harness.operator.interrupted, "the possibly-running host object must be stopped"
    assert sessions[0].host_session_ref == harness.operator.interrupted[0].host_session_ref


async def test_crash_between_open_phases_leaves_a_failed_session_and_a_reopen_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BLOCKER 1: the durable ``failed`` marker frees the one-active-session index."""
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)

    async def crash(_session: AgentSessionV1, _timestamp: datetime) -> AgentSessionV1:
        raise RuntimeError("crash between phases")

    monkeypatch.setattr(harness.service, "_transition_prepared", crash)
    with pytest.raises(RuntimeError):
        await harness.open(handoff=handoff, task=task, idempotency_key="crash-open")
    monkeypatch.undo()

    sessions = await harness.sessions.list_for_task(task.id)
    assert len(sessions) == 1
    assert sessions[0].state is AgentSessionState.FAILED
    assert harness.operator.interrupted

    reopened = await harness.open(handoff=handoff, task=task, idempotency_key="reopen-after-crash")
    assert reopened.session.state is AgentSessionState.RUNNING
    persisted = await harness.sessions.get(sessions[0].id)
    assert persisted is not None and persisted.state is AgentSessionState.FAILED


async def test_human_close_route_frees_an_orphaned_ready_session() -> None:
    """BLOCKER 1(c): the human escape hatch is mounted and frees the task."""
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    orphan = _ready_session(harness, task, UUID(handoff.handoff_id))
    await harness.sessions.add(orphan)
    app = _api_app(harness, LocalIdentityProvider("dev-analyst"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        closed = await client.post(
            f"/api/investigations/{harness.investigation_id}/agent-sessions/{orphan.id}/close",
            json={"reason": "orphaned provisioning session"},
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["state"] == "closed"

        reopened = await client.post(
            f"/api/investigations/{harness.investigation_id}/agent-sessions",
            headers={"Idempotency-Key": "open-after-human-close"},
            json={
                "task_id": str(task.id),
                "adapter_kind": "codex_cli",
                "handoff_id": handoff.handoff_id,
                "executable": EXECUTABLE,
            },
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["session"]["state"] == "running"

    # The close ran without a wired host runtime: the effect is marked, not silent.
    stored = await harness.sessions.get(orphan.id)
    assert stored is not None
    assert stored.reconciliation_outcome() is SessionReconcileOutcome.LOST_UNRECONCILED


async def test_close_route_refuses_an_operator_client_identity() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    orphan = _ready_session(harness, task, UUID(handoff.handoff_id))
    await harness.sessions.add(orphan)
    app = _api_app(harness, _OperatorClientIdentity())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            f"/api/investigations/{harness.investigation_id}/agent-sessions/{orphan.id}/close",
            json={"reason": "not a human"},
        )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "verified_human_operator_required"


# -------------------------------------------------- network isolation (R03 B1)


async def test_unattested_network_mode_refuses_before_any_host_prepare() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    harness.operator.capabilities = harness.operator.capabilities.model_copy(
        update={"confinement": None}
    )

    with pytest.raises(SessionNetworkIsolationUnavailableError) as exc_info:
        await harness.service.open_session(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(handoff.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="open-no-attestation",
        )
    assert exc_info.value.code == "agent_session_network_isolation_unavailable"
    assert exc_info.value.status_code == 503
    assert exc_info.value.details == {"reason": "attestation_absent"}
    assert [call for call, _ in harness.host_calls] == ["probe"]
    assert harness.operator.prepared == []
    assert harness.operator.started == []
    assert await harness.sessions.list_for_task(task.id) == ()


def test_host_network_unsafe_is_refused_at_configuration_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M24.1 CFG-1: isolation is the only mode; an old value never degrades silently."""
    assert Settings(_env_file=None).agent_sessions_cli_network_mode == "isolated_egress"

    monkeypatch.setenv("AGENT_SESSIONS_CLI_NETWORK_MODE", "host_network_unsafe")
    with pytest.raises(ValidationError, match="host_network_unsafe is no longer supported"):
        Settings(_env_file=None)

    monkeypatch.delenv("AGENT_SESSIONS_CLI_NETWORK_MODE")
    with pytest.raises(ValidationError, match="host_network_unsafe is no longer supported"):
        Settings(_env_file=None, agent_sessions_cli_network_mode="host_network_unsafe")


def test_session_service_refuses_an_unsafe_cli_network_mode() -> None:
    harness = _Harness()
    with pytest.raises(ValueError, match="cli_network_mode must be isolated_egress"):
        SessionService(
            uow_factory=lambda: harness.uow,
            deriver=harness.deriver,
            cli_network_mode="host_network_unsafe",
        )


async def test_non_isolated_host_attestation_is_always_refused() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    handoff = await harness.add_handoff(task)
    harness.operator.capabilities = _capabilities(
        confinement=OperatorConfinementV1(
            profile="bwrap-reasoning-session",
            bwrap_ok=True,
            netns=False,
            network_mode="isolated_egress",
            loopback_isolated=False,
        )
    )

    with pytest.raises(SessionNetworkIsolationUnavailableError) as exc_info:
        await harness.service.open_session(
            harness.investigation_id,
            task_id=task.id,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=UUID(handoff.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN_ACTOR,
            idempotency_key="open-not-isolated-refused",
        )
    assert exc_info.value.details["reason"] == "loopback_not_isolated"
    assert harness.operator.prepared == []


# --------------------------------------------------- capabilities route (B3)


async def test_capabilities_route_is_human_only_and_returns_probe_facts() -> None:
    harness = _Harness()
    app = _api_app(harness, LocalIdentityProvider("dev-analyst"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/investigations/agent-sessions/capabilities",
            params={"adapter_kind": "codex_cli", "executable": EXECUTABLE},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["adapter_kind"] == "codex_cli"
    assert body["executable"] == EXECUTABLE
    assert body["available"] is True
    assert body["supports_resume"] is True
    assert body["modes"] == ["exec", "apply"]
    assert len(body["capability_sha256"]) == 64
    assert body["confinement"]["network_mode"] == "isolated_egress"
    assert body["confinement"]["loopback_isolated"] is True
    assert "secret" not in response.text.lower()

    operator_client_app = _api_app(harness, _OperatorClientIdentity())
    async with AsyncClient(
        transport=ASGITransport(app=operator_client_app), base_url="http://test"
    ) as client:
        refused = await client.get(
            "/api/investigations/agent-sessions/capabilities",
            params={"adapter_kind": "codex_cli", "executable": EXECUTABLE},
        )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "verified_human_operator_required"


async def test_capabilities_route_maps_an_unreachable_host_to_workspace_local_unavailable() -> None:
    harness = _Harness()
    app = _api_app(harness, LocalIdentityProvider("dev-analyst"))

    class _UnreachableOperator(_FakeOperator):
        async def probe_capabilities(
            self, request: ProbeCapabilitiesRequest
        ) -> OperatorCapabilitiesV1:
            raise WorkspaceLocalUnavailableError("workspace agent is unreachable")

    app.state.agentic_operator_port = _UnreachableOperator()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/investigations/agent-sessions/capabilities",
            params={"adapter_kind": "codex_cli", "executable": EXECUTABLE},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "workspace_local_unavailable"

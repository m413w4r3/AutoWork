"""Oracles for M24-X1 transfers and note-referenced instructions.

Service-level scenarios A05/D04/D05/D08/D09 run over the canonical in-memory
ports: the real ``SessionService``, task engine, handoff service and transfer
service, with a deterministic fake operator and a fake host workspace page
feed.  The separate host-contract test at the end drives the REAL sidecar
supervisor (fake CLI profile) through the real backend adapters; no live CLI,
provider or network is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from cti_app.application.agent_session_verification import HmacSessionSecretDeriver
from cti_app.application.agentic_sessions import (
    EnqueuedInstructionV1,
    SessionSecret,
    SessionService,
)
from cti_app.application.investigation_handoffs import (
    HandoffCreateRequest,
    HandoffRecord,
    InvestigationHandoffService,
)
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    EvidenceObservationRepository,
    FindingRepository,
    InMemoryAgentSessionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemoryInvestigationHandoffRepository,
    InMemoryInvestigationPlanRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryToolInvocationRepository,
    InMemoryWorkspaceBindingRepository,
    InMemoryWorkspaceFileEventRepository,
    InMemoryWorkspaceFileRevisionRepository,
    InMemoryWorkspaceSyncCursorRepository,
    InvestigationEnvelopeRepository,
    InvestigationHandoffRepository,
    InvestigationPlanRepository,
    InvestigationTaskRepository,
    ToolInvocationRepository,
    WorkspaceBindingRepository,
    WorkspaceFileEventRepository,
    WorkspaceFileRevisionRepository,
    WorkspaceSyncCursorRepository,
)
from cti_app.application.investigation_persistence import (
    HandoffRecord as PersistedHandoffRecord,
)
from cti_app.application.investigation_tasks import TaskSessionRelease
from cti_app.application.investigation_transfers import (
    InvestigationTransferService,
    NoteReferenceError,
    TransferStaleVersionError,
    TransferTaskRequest,
    WorkspaceNoteCatalog,
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.application.workspace_agent import (
    EventsPageV1,
    EventV1,
    FileStateV1,
    ScanResultV1,
    SnapshotPageV1,
    WorkspaceAgentPort,
    WorkspaceBindingV1,
    WorkspaceFileRevisionV1,
    workspace_revision_id,
)
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
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_handoff import (
    HandoffActor,
    HandoffActorKind,
    HandoffRef,
    HandoffRefKind,
    HandoffTaskSnapshot,
    HandoffWork,
    HandoffWorkItem,
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
LATER = NOW + timedelta(minutes=10)
SESSION_KEY = "0123456789abcdef0123456789abcdef"
EXECUTABLE = "/usr/local/bin/agent"
INVESTIGATION_ID = uuid4()
WORKSPACE_ID = "ws-" + "a" * 32
NOTE_PATH = "notes/humaines/analyse.md"
HUMAN = TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test")
CODEX_ID = "operator:codex_cli"
CLAUDE_ID = "operator:claude_code"


class _ArticleSnapshots:
    async def resolve(self, synthesis_artifact_id: UUID) -> Any:
        return SimpleNamespace(tlp="clear", external_llm_allowed=True)


class _OsintPolicyService:
    async def effective_for_investigation(self, investigation_id: UUID) -> Any:
        return None


class _CompositionFactory:
    """The minimal canonical composition the disclosure gate reads."""

    @property
    def article_snapshots(self) -> _ArticleSnapshots:
        return _ArticleSnapshots()

    @property
    def osint_open_policy_service(self) -> _OsintPolicyService:
        return _OsintPolicyService()


async def _allow_disclosure(investigation_id: UUID) -> None:
    """The transfer tests exercise the flow; the gate has its own oracles."""
    return None


class _InvestigationRepository:
    def __init__(self, investigation_id: UUID) -> None:
        self._investigation = SimpleNamespace(
            id=investigation_id,
            subject_id=uuid4(),
            synthesis_artifact_id=uuid4(),
        )

    async def get(self, investigation_id: UUID) -> Any:
        return self._investigation if investigation_id == self._investigation.id else None


class _DecisionRepository:
    def __init__(self) -> None:
        self.items: list[AnalystDecision] = []

    async def append(self, decision: AnalystDecision) -> None:
        self.items.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _FakeWorkspaceAgent:
    """A deterministic page feed over the revisions a transfer must ingest."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.files: list[FileStateV1] = []
        self.scans = 0
        self.calls: list[str] = []

    def add_revision(
        self,
        *,
        path: str,
        sha256: str,
        observed_at: datetime = NOW,
        origin: str = "ui",
        task_id: UUID | None = None,
    ) -> FileStateV1:
        revision_id = workspace_revision_id(
            workspace_id=self.workspace_id, path=path, sha256=sha256
        )
        state = FileStateV1(
            path=path,
            revision_id=revision_id,
            sha256=sha256,
            size=128,
            mode="0644",
            mtime_ns=0,
            origin=cast(Any, origin),
            observed_at=observed_at,
            policy_class="human_editable",
            task_id=str(task_id) if task_id is not None else None,
        )
        self.files.append(state)
        return state

    async def scan_workspace(
        self,
        workspace_id: str,
        *,
        full: bool = False,
        idempotency_key: str | None = None,
    ) -> ScanResultV1:
        assert workspace_id == self.workspace_id
        self.scans += 1
        self.calls.append("scan")
        return ScanResultV1(
            generation=1,
            latest_cursor=len(self.files),
            emitted=len(self.files),
            policy_violations=0,
        )

    async def events(
        self, workspace_id: str, *, after: int | None = None, limit: int = 500
    ) -> EventsPageV1:
        assert workspace_id == self.workspace_id
        self.calls.append("events")
        if after is not None:
            return EventsPageV1(
                generation=1, events=[], next_cursor=after, oldest_retained=0, resync_required=False
            )
        events: list[EventV1] = []
        for seq, state in enumerate(self.files, start=1):
            events.append(
                EventV1(
                    event_id=f"evt-{seq}",
                    seq=seq,
                    generation=1,
                    kind="file_created",
                    path=state.path,
                    revision_id=state.revision_id,
                    sha256=state.sha256,
                    size=state.size,
                    origin=state.origin,
                    observed_at=state.observed_at,
                )
            )
        return EventsPageV1(
            generation=1,
            events=events,
            next_cursor=len(events),
            oldest_retained=0,
            resync_required=False,
        )

    async def snapshot(
        self, workspace_id: str, *, page_token: str | None = None, limit: int = 500
    ) -> SnapshotPageV1:
        assert workspace_id == self.workspace_id
        self.calls.append("snapshot")
        return SnapshotPageV1(generation=1, files=self.files, next_page_token=None)


def _capabilities(
    *, adapter_kind: OperatorAdapterKind = OperatorAdapterKind.CODEX_CLI
) -> OperatorCapabilitiesV1:
    return OperatorCapabilitiesV1(
        adapter_kind=adapter_kind,
        executable=EXECUTABLE,
        version="agent 1.2.3",
        modes=("exec", "resume"),
        supports_resume=True,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=False,
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
                resolved_version="agent 1.2.3",
            ),
        ),
    )


class _FakeOperator:
    """Deterministic ``AgenticOperatorPort`` for both adapter kinds."""

    def __init__(self) -> None:
        self.prepared: list[PrepareSessionRequest] = []
        self.started: list[StartSessionRequest] = []
        self.interrupted: list[InterruptSessionRequest] = []
        self.reconciled: list[ReconcileSessionRequest] = []
        self.resumed: list[ResumeSessionRequest] = []
        self.instructions: list[SendInstructionRequest] = []
        self.reconcile_outcome = SessionReconcileOutcome.SESSION_ALIVE
        self.interrupt_stopped = True
        self.started_refs: dict[UUID, str] = {}

    def capabilities(self, kind: OperatorAdapterKind) -> OperatorCapabilitiesV1:
        return _capabilities(adapter_kind=kind)

    async def probe_capabilities(self, request: ProbeCapabilitiesRequest) -> OperatorCapabilitiesV1:
        return self.capabilities(request.adapter_kind)

    async def prepare_session(self, request: PrepareSessionRequest) -> PreparedOperatorSessionV1:
        self.prepared.append(request)
        capabilities = self.capabilities(request.adapter_kind)
        ref = str(request.session_id)
        self.started_refs[request.session_id] = ref
        return PreparedOperatorSessionV1(
            session_id=request.session_id,
            investigation_id=request.investigation_id,
            task_id=request.task_id,
            adapter_kind=request.adapter_kind,
            host_session_ref=ref,
            capabilities=capabilities,
            prepared_at=NOW,
        )

    async def start_session(self, request: StartSessionRequest) -> SessionProcessStatusV1:
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
            outcome=InstructionDeliveryOutcome.ACCEPTED,
            observed_at=NOW,
        )

    async def request_handoff(self, request: RequestHandoffRequest) -> HandoffRequestReceiptV1:
        raise AssertionError("a transfer never asks the host for a handoff")

    async def interrupt_session(self, request: InterruptSessionRequest) -> InterruptSessionResultV1:
        self.interrupted.append(request)
        return InterruptSessionResultV1(
            session_id=request.session_id,
            stopped=self.interrupt_stopped,
            observed_at=NOW,
        )

    async def resume_session(self, request: ResumeSessionRequest) -> ResumeSessionResultV1:
        self.resumed.append(request)
        return ResumeSessionResultV1(
            session_id=request.session_id,
            resumed=True,
            host_session_ref=request.host_session_ref,
            observed_at=NOW,
        )

    async def collect_results(self, request: CollectResultsRequest) -> OperatorResultsV1:
        return OperatorResultsV1(session_id=request.session_id, observed_at=NOW)

    async def reconcile_session(self, request: ReconcileSessionRequest) -> SessionReconciliationV1:
        self.reconciled.append(request)
        values: dict[str, Any] = {
            "session_id": request.session_id,
            "outcome": self.reconcile_outcome,
            "observed_at": NOW,
            "detail": "fake reconcile",
        }
        if self.reconcile_outcome is SessionReconcileOutcome.LOST_WITH_HANDOFF:
            values["checkpoint_ref"] = "checkpoint-1"
            values["checkpoint_sha256"] = "c" * 64
        return SessionReconciliationV1(**values)


class _FakeSecretDelivery:
    def __init__(self) -> None:
        self.deliveries: list[UUID] = []

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
        self.deliveries.append(session_id)


class _FakeUnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.analyst_investigations: AnalystInvestigationRepository = harness.investigations
        self.analyst_decisions: AnalystDecisionRepository = harness.decisions
        self.investigation_tasks: InvestigationTaskRepository = harness.tasks
        self.investigation_handoffs: InvestigationHandoffRepository = harness.handoffs
        self.investigation_plans: InvestigationPlanRepository = harness.plans
        self.agent_sessions: AgentSessionRepository = harness.sessions
        self.workspace_bindings: WorkspaceBindingRepository = harness.bindings
        self.workspace_file_revisions: WorkspaceFileRevisionRepository = harness.revisions
        self.workspace_file_events: WorkspaceFileEventRepository = harness.events
        self.workspace_sync_cursors: WorkspaceSyncCursorRepository = harness.cursors
        self.evidence_observations: EvidenceObservationRepository = harness.observations
        self.investigation_findings: FindingRepository = harness.findings
        self.tool_invocations: ToolInvocationRepository = harness.invocations
        self.investigation_envelopes: InvestigationEnvelopeRepository = harness.envelopes

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Harness:
    def __init__(self) -> None:
        self.investigations = _InvestigationRepository(INVESTIGATION_ID)
        self.decisions = _DecisionRepository()
        self.tasks = InMemoryInvestigationTaskRepository(clock=lambda: NOW)
        self.handoffs = InMemoryInvestigationHandoffRepository()
        self.plans = InMemoryInvestigationPlanRepository()
        self.sessions = InMemoryAgentSessionRepository()
        self.bindings = InMemoryWorkspaceBindingRepository()
        self.revisions = InMemoryWorkspaceFileRevisionRepository()
        self.events = InMemoryWorkspaceFileEventRepository()
        self.cursors = InMemoryWorkspaceSyncCursorRepository()
        self.observations = InMemoryEvidenceObservationRepository()
        self.findings = InMemoryFindingRepository()
        self.invocations = InMemoryToolInvocationRepository()
        self.envelopes = InMemoryInvestigationEnvelopeRepository()
        self.uow = _FakeUnitOfWork(self)
        self.workspace = _FakeWorkspaceAgent(WORKSPACE_ID)
        self.operator = _FakeOperator()
        self.delivery = _FakeSecretDelivery()
        self.deriver = HmacSessionSecretDeriver(SESSION_KEY)
        self.sessions_service = SessionService(
            uow_factory=lambda: self.uow,
            deriver=self.deriver,
            operator=self.operator,
            secret_delivery=self.delivery,
            disclosure=_allow_disclosure,
            clock=lambda: NOW,
        )
        self.handoffs_service = InvestigationHandoffService(
            uow_factory=lambda: self.uow, clock=lambda: NOW
        )
        self.host_releases: list[list[TaskSessionRelease]] = []
        self.transfers = InvestigationTransferService(
            uow_factory=lambda: self.uow,
            workspace_agent=cast(WorkspaceAgentPort, self.workspace),
            operator=self.operator,
            secret_delivery=self.delivery,
            deriver=self.deriver,
            disclosure=_allow_disclosure,
            release_host=self._release_host,
            clock=lambda: NOW,
        )
        self.notes = WorkspaceNoteCatalog(uow_factory=lambda: self.uow)
        self.instruction_service = SessionService(
            uow_factory=lambda: self.uow,
            deriver=self.deriver,
            operator=self.operator,
            secret_delivery=self.delivery,
            note_resolver=self.notes,
            disclosure=_allow_disclosure,
            clock=lambda: NOW,
        )

    async def _release_host(self, releases: Sequence[TaskSessionRelease]) -> None:
        self.host_releases.append(list(releases))

    async def bind_workspace(self, *, workspace_id: str = WORKSPACE_ID) -> None:
        await self.bindings.add_if_absent(
            WorkspaceBindingV1(
                workspace_id=workspace_id,
                investigation_id=INVESTIGATION_ID,
                subject_id=uuid4(),
                delivery_code="IR202611",
                article_slug="loader-article",
                synthesis_sha256="e" * 64,
                created_at=NOW,
            )
        )

    async def approve_envelope(self) -> None:
        from cti_app.domain.investigation import (
            BudgetDimension,
            DisclosureBounds,
            InvestigationBudget,
            InvestigationEnvelope,
        )

        await self.envelopes.save_if_version(
            InvestigationEnvelope(
                investigation_id=INVESTIGATION_ID,
                allowed_providers=("vt",),
                budget=InvestigationBudget(
                    limits={
                        BudgetDimension.TOOL_SECONDS: 1200,
                        BudgetDimension.CONTEXT_BYTES: 4 * 1024 * 1024,
                    }
                ),
                acquire_hits_allowed=False,
                disclosure=DisclosureBounds(tlp=TLPLevel.GREEN, external_llm_allowed=True),
                approval_decision_id=uuid4(),
                approval_actor_id="analyst:test",
            ),
            expected_version=None,
        )

    async def seed_task(
        self,
        *,
        status: TaskStatus = TaskStatus.READY,
        status_version: int = 1,
        assigned_actor_kind: TaskActorKind | None = None,
        assigned_actor_id: str | None = None,
        task_id: UUID | None = None,
        task_key: str = "lead-loader",
        lease_fence: int = 0,
        lease_session_id: UUID | None = None,
        lease_id: UUID | None = None,
        paths: tuple[str, ...] = ("notes/humaines", "results"),
    ) -> InvestigationTaskV1:
        task = InvestigationTaskV1(
            id=task_id or uuid4(),
            investigation_id=INVESTIGATION_ID,
            task_key=task_key,
            title="Inspect the loader",
            question="Which network gate does the loader use?",
            objective="Identify the first-stage C2 gate.",
            priority=TaskPriority.HIGH,
            status=status,
            status_version=status_version,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=HUMAN,
            created_at=NOW,
            updated_at=NOW,
            assigned_actor_kind=assigned_actor_kind,
            assigned_actor_id=assigned_actor_id,
            allowed_workspace_paths=paths,
            required_capabilities=("autowork-yara-bridge-workflow",),
            lease_id=lease_id,
            lease_session_id=lease_session_id,
            lease_fence=lease_fence,
            lease_expires_at=LATER if lease_id is not None else None,
            heartbeat_at=NOW if lease_id is not None else None,
        )
        return await self.tasks.add(task)

    async def add_revision(
        self,
        *,
        path: str = NOTE_PATH,
        sha256: str,
        observed_at: datetime = NOW,
        origin: str = "ui",
    ) -> WorkspaceFileRevisionV1:
        revision = WorkspaceFileRevisionV1(
            workspace_id=WORKSPACE_ID,
            revision_id=workspace_revision_id(workspace_id=WORKSPACE_ID, path=path, sha256=sha256),
            path=path,
            sha256=sha256,
            size=128,
            mode="0644",
            mtime_ns=0,
            observed_at=observed_at,
            origin=cast(Any, origin),
        )
        stored, _created = await self.revisions.append_if_absent(revision)
        return stored

    async def add_observation(self, *, sha256: str = "b" * 64) -> EvidenceObservation:
        observation = EvidenceObservation(
            investigation_id=INVESTIGATION_ID,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256=sha256,
            source="workspace_import",
            source_version="work-contribution.v1",
            observed_at=NOW,
            object_type="sample",
            object_id="loader",
        )
        return await self.observations.append_if_absent(observation)

    async def add_finding(self, observation: EvidenceObservation) -> InvestigationFinding:
        finding = InvestigationFinding(
            investigation_id=INVESTIGATION_ID,
            kind=FindingKind.NETWORK_BEHAVIOUR,
            assessment=FindingAssessment.SUPPORTED,
            statement="The loader resolves its gate on demand.",
            observation_refs=(observation.id,),
            created_at=NOW,
        )
        return await self.findings.append_version(finding, expected_previous_id=None)

    async def add_decision(self, task_id: UUID) -> AnalystDecision:
        decision = AnalystDecision(
            investigation_id=INVESTIGATION_ID,
            decision_type=AnalystDecisionType.TASK_RESULT_ACCEPT,
            target_type=AnalystDecisionTargetType.TASK,
            target_id=task_id,
            actor_id="analyst:test",
            reason="Accept the interim result for the transfer",
            correlation_id=f"transfer-{uuid4()}",
            occurred_at=NOW,
        )
        await self.decisions.append(decision)
        return decision

    async def add_consultation(self) -> ToolInvocation:
        advice = EvidenceObservation(
            investigation_id=INVESTIGATION_ID,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256=hashlib.sha256(f"advice-{uuid4()}".encode()).hexdigest(),
            source="advisor.consult.response",
            source_version="advisor-consult.v1",
            observed_at=NOW,
            object_type="advisor_response_received",
            object_id="advice",
        )
        stored_advice = await self.observations.append_if_absent(advice)
        invocation = ToolInvocation(
            investigation_id=INVESTIGATION_ID,
            operation_key=f"transfer-consult-{uuid4()}",
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            normalized_parameters={"consultation_type": "interpretation", "question": "gate?"},
            purpose="Ask Bridge how to read the loader gate",
            status=ToolInvocationStatus.SUCCEEDED,
            response_refs=(stored_advice.id,),
            created_at=NOW,
            updated_at=NOW,
        )
        return await self.invocations.add_if_absent(invocation)

    async def seed_previous_handoff(
        self, task: InvestigationTaskV1, observation: EvidenceObservation
    ) -> HandoffRecord:
        handoff = InvestigationHandoffV1(
            handoff_id=str(uuid4()),
            investigation_id=str(INVESTIGATION_ID),
            task_id=str(task.id),
            version=1,
            created_at=NOW,
            from_actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
            to_actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=CODEX_ID),
            task=HandoffTaskSnapshot(
                status=task.status,
                status_version=task.status_version,
                question=task.question,
                objective=task.objective,
                assigned_actor_kind=(
                    HandoffActorKind.OPERATOR
                    if task.assigned_actor_kind is TaskActorKind.OPERATOR
                    else None
                ),
                assigned_actor_id=task.assigned_actor_id,
            ),
            work=HandoffWork(
                hypotheses=(
                    HandoffWorkItem(
                        text="The gate is resolved on demand.",
                        refs=(HandoffRef(kind=HandoffRefKind.OBSERVATION, id=str(observation.id)),),
                    ),
                ),
                established_results=(HandoffWorkItem(text="Static string extraction completed."),),
            ),
            next_action="Continue from the previous analysis.",
        )
        record = PersistedHandoffRecord(
            handoff=handoff,
            idempotency_key="seed-handoff",
            request_sha256="d" * 64,
            created_by=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
        )
        stored, _created = await self.handoffs.append_if_absent(record)
        return stored

    async def open_session(
        self,
        task: InvestigationTaskV1,
        *,
        handoff: InvestigationHandoffV1 | None = None,
        adapter_kind: OperatorAdapterKind = OperatorAdapterKind.CODEX_CLI,
        idempotency_key: str = "open-session",
    ) -> AgentSessionV1:
        bound = handoff
        if bound is None:
            bound = (
                await self.handoffs_service.create(
                    INVESTIGATION_ID,
                    actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
                    idempotency_key=f"handoff-{idempotency_key}",
                    request=HandoffCreateRequest(
                        task_id=task.id,
                        to_actor=HandoffActor(
                            kind=HandoffActorKind.OPERATOR,
                            actor_id=(
                                CODEX_ID
                                if adapter_kind is OperatorAdapterKind.CODEX_CLI
                                else CLAUDE_ID
                            ),
                        ),
                        next_action="Continue the analysis.",
                    ),
                )
            ).handoff
        prepared = await self.sessions_service.open_session(
            INVESTIGATION_ID,
            task_id=task.id,
            adapter_kind=adapter_kind,
            handoff_id=UUID(bound.handoff_id),
            executable=EXECUTABLE,
            actor=HUMAN,
            idempotency_key=idempotency_key,
        )
        return prepared.session

    def transfer_request(
        self,
        *,
        to_actor_kind: str = "operator",
        adapter_kind: OperatorAdapterKind | None = OperatorAdapterKind.CLAUDE_CODE,
        instruction: str | None = "Deepen the network-protocol check.",
        expected_status_version: int = 1,
        open_session: bool = False,
        model: str | None = "claude-sonnet-5-5",
    ) -> TransferTaskRequest:
        return TransferTaskRequest(
            to_actor_kind=cast(Any, to_actor_kind),
            to_adapter_kind=adapter_kind,
            instruction=instruction,
            expected_status_version=expected_status_version,
            open_session=open_session,
            executable=EXECUTABLE if open_session else None,
            model=model,
        )

    def session_row(self, session_id: UUID) -> AgentSessionV1:
        return self.sessions._sessions[session_id]

    def task_row(self, task_id: UUID) -> InvestigationTaskV1:
        return self.tasks._tasks[task_id]


async def _scenario_harness() -> _Harness:
    harness = _Harness()
    await harness.bind_workspace()
    return harness


# --------------------------------------------------------------------- D05


async def test_transfer_to_claude_code_carries_objectives_notes_and_opens_session() -> None:
    harness = await _scenario_harness()
    task = await harness.seed_task()
    old_revision = harness.workspace.add_revision(path=NOTE_PATH, sha256="1" * 64, observed_at=NOW)
    latest_revision = harness.workspace.add_revision(
        path=NOTE_PATH, sha256="2" * 64, observed_at=LATER
    )
    observation = await harness.add_observation()
    finding = await harness.add_finding(observation)
    decision = await harness.add_decision(task.id)
    consultation = await harness.add_consultation()

    result = await harness.transfers.transfer(
        INVESTIGATION_ID,
        task_id=task.id,
        actor=HUMAN,
        idempotency_key="transfer-1",
        request=harness.transfer_request(
            instruction="Deepen the protocol check on the gate.",
            open_session=True,
        ),
    )

    assert result.destination.actor_id == CLAUDE_ID
    assert result.task.status is TaskStatus.RUNNING
    assert (result.task.assigned_actor_kind, result.task.assigned_actor_id) == (
        TaskActorKind.OPERATOR,
        CLAUDE_ID,
    )
    handoff = result.handoff.handoff
    assert handoff.version == 1
    assert handoff.next_action == "Deepen the protocol check on the gate."
    assert handoff.to_actor.actor_id == CLAUDE_ID
    assert [item.file_relpath for item in handoff.files] == [NOTE_PATH]
    assert handoff.files[0].workspace_revision_id == latest_revision.revision_id
    assert handoff.files[0].workspace_revision_id != old_revision.revision_id
    assert {item.id for item in handoff.refs.observations} == {str(observation.id)}
    assert {item.version_id for item in handoff.refs.findings} == {str(finding.id)}
    assert {item.analyst_decision_id for item in handoff.refs.decisions} == {str(decision.id)}
    assert {item.invocation_id for item in handoff.refs.consultations} == {str(consultation.id)}
    assert result.previous_handoff_id is None
    assert result.sync is not None
    assert result.sync.inserted_events == 2
    assert result.sync.inserted_revisions == 2
    assert result.session is not None
    assert result.session.adapter_kind is OperatorAdapterKind.CLAUDE_CODE
    assert result.session.state is AgentSessionState.RUNNING
    assert result.session.handoff_id == UUID(handoff.handoff_id)
    stored = await harness.revisions.list_for_workspace(WORKSPACE_ID)
    assert {item.revision_id for item in stored} == {
        old_revision.revision_id,
        latest_revision.revision_id,
    }


async def test_transfer_to_human_creates_handoff_without_session() -> None:
    harness = await _scenario_harness()
    task = await harness.seed_task()
    harness.workspace.add_revision(path=NOTE_PATH, sha256="3" * 64)

    result = await harness.transfers.transfer(
        INVESTIGATION_ID,
        task_id=task.id,
        actor=HUMAN,
        idempotency_key="transfer-human",
        request=harness.transfer_request(
            to_actor_kind="human", adapter_kind=None, instruction="I take it back."
        ),
    )

    assert result.task.status is TaskStatus.ASSIGNED
    assert (result.task.assigned_actor_kind, result.task.assigned_actor_id) == (
        TaskActorKind.HUMAN,
        HUMAN.actor_id,
    )
    assert result.handoff.handoff.to_actor.kind is HandoffActorKind.HUMAN
    assert result.session is None
    assert await harness.sessions.list_for_task(task.id) == ()


# --------------------------------------------------------------------- D04


async def test_interrupted_codex_handoff_keeps_analyses() -> None:
    harness = await _scenario_harness()
    task = await harness.seed_task(
        status=TaskStatus.ASSIGNED,
        status_version=2,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=CODEX_ID,
    )
    observation = await harness.add_observation()
    previous = await harness.seed_previous_handoff(task, observation)
    await harness.add_revision(path=NOTE_PATH, sha256="4" * 64)
    opened = await harness.open_session(task, handoff=previous.handoff)
    assert opened.state is AgentSessionState.RUNNING
    before_starts = len(harness.operator.started)
    before_resumes = len(harness.operator.resumed)

    result = await harness.transfers.transfer(
        INVESTIGATION_ID,
        task_id=task.id,
        actor=HUMAN,
        idempotency_key="transfer-d04",
        request=harness.transfer_request(
            expected_status_version=harness.task_row(task.id).status_version,
            open_session=True,
        ),
    )

    old = await harness.sessions.get(opened.id)
    assert old is not None and old.state is AgentSessionState.CLOSED
    assert len(harness.operator.interrupted) == 1
    assert len(harness.operator.reconciled) == 1
    assert len(harness.operator.resumed) == before_resumes
    handoff = result.handoff.handoff
    assert handoff.version == 2
    assert result.previous_handoff_id == previous.handoff.handoff_id
    assert result.previous_handoff_version == 1
    assert result.previous_freshness is not None
    assert result.previous_freshness.state == "fresh"
    assert [item.text for item in handoff.work.hypotheses] == ["The gate is resolved on demand."]
    assert [item.text for item in handoff.work.established_results] == [
        "Static string extraction completed."
    ]
    assert handoff.work.hypotheses[0].refs[0].id == str(observation.id)
    assert result.session is not None
    assert result.session.id != opened.id
    assert len(harness.operator.started) == before_starts + 1
    assert result.task.lease_fence >= 2


# --------------------------------------------------------------------- D09


async def test_lost_session_is_reconciled_and_never_relaunched() -> None:
    harness = await _scenario_harness()
    lease_id = uuid4()
    session_id = uuid4()
    task = await harness.seed_task(
        status=TaskStatus.RUNNING,
        status_version=3,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=CODEX_ID,
        lease_fence=1,
        lease_id=lease_id,
        lease_session_id=session_id,
    )
    session = AgentSessionV1(
        id=session_id,
        investigation_id=INVESTIGATION_ID,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.RUNNING,
        state_version=2,
        capability_probe=_capabilities().to_payload(),
        capability_sha256=_capabilities().capability_sha256,
        probed_at=NOW,
        auth_secret_nonce=b"n" * 32,
        auth_secret_hash="f" * 64,
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key="seed-session",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=str(session_id),
        task_lease_id=lease_id,
        task_lease_fence=1,
    )
    await harness.sessions.add(session)
    harness.operator.reconcile_outcome = SessionReconcileOutcome.LOST_UNRECONCILED
    await harness.add_revision(path=NOTE_PATH, sha256="5" * 64)

    result = await harness.transfers.transfer(
        INVESTIGATION_ID,
        task_id=task.id,
        actor=HUMAN,
        idempotency_key="transfer-d09",
        request=harness.transfer_request(
            expected_status_version=harness.task_row(task.id).status_version,
            instruction="Resume from the lost session.",
        ),
    )

    lost = await harness.sessions.get(session_id)
    assert lost is not None
    assert lost.state is AgentSessionState.LOST
    assert lost.close_reason == SessionReconcileOutcome.LOST_UNRECONCILED.value
    assert len(harness.operator.reconciled) == 1
    assert harness.operator.resumed == []
    assert harness.operator.started == []
    assert harness.operator.interrupted == []
    assert result.task.status is TaskStatus.ASSIGNED
    assert result.task.assigned_actor_id == CLAUDE_ID
    assert result.task.lease_id is None
    assert result.task.lease_fence == 2
    assert result.session is None
    events = await harness.tasks.list_events(task.id)
    assert [event.event_type.value for event in events] == ["status_changed", "assigned"]
    assert events[0].to_status is TaskStatus.BLOCKED
    assert events[1].to_status is TaskStatus.ASSIGNED
    # B2: the blocking transition released the lease and committed the closed
    # session before the host teardown was requested.
    assert harness.host_releases, "a blocking transfer must flush its host release"
    assert harness.host_releases[0][0].session_id == session_id


# --------------------------------------------------------------------- A05


async def test_human_note_modified_mid_session_reaches_the_agent() -> None:
    harness = await _scenario_harness()
    task = await harness.seed_task(
        status=TaskStatus.ASSIGNED,
        status_version=2,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=CODEX_ID,
    )
    first = await harness.add_revision(path=NOTE_PATH, sha256="6" * 64, observed_at=NOW)
    opened = await harness.open_session(task)
    assert opened.state is AgentSessionState.RUNNING

    enqueued = await harness.instruction_service.enqueue_instruction_with_notes(
        INVESTIGATION_ID,
        opened.id,
        text="Check the latest human note before pivoting.",
        actor=HUMAN,
        idempotency_key="instruction-a05",
        note_refs=(NOTE_PATH,),
    )
    assert isinstance(enqueued, EnqueuedInstructionV1)
    assert enqueued.delivered_live is False
    assert enqueued.queue_position == 1
    assert first.revision_id in enqueued.instruction.text
    assert first.sha256 in enqueued.instruction.text
    assert harness.operator.instructions == []

    second = await harness.add_revision(path=NOTE_PATH, sha256="7" * 64, observed_at=LATER)
    acknowledged = await harness.instruction_service.acknowledge_instructions_with_notes(
        INVESTIGATION_ID,
        opened.id,
        instruction_ids=(enqueued.instruction.id,),
        actor=TaskActor(kind=TaskActorKind.OPERATOR, actor_id=CODEX_ID),
    )
    assert len(acknowledged) == 1
    view = acknowledged[0]
    assert view.note_diffs == (
        {
            "path": NOTE_PATH,
            "queued_revision_id": first.revision_id,
            "queued_sha256": first.sha256,
            "current_revision_id": second.revision_id,
            "current_sha256": second.sha256,
        },
    )
    assert second.revision_id in view.delivery_text
    assert "notes modifiées" in view.delivery_text

    # The same modification also reaches a new agent through the transfer handoff.
    result = await harness.transfers.transfer(
        INVESTIGATION_ID,
        task_id=task.id,
        actor=HUMAN,
        idempotency_key="transfer-a05",
        request=harness.transfer_request(
            expected_status_version=harness.task_row(task.id).status_version,
            instruction="Continue from the revised note.",
        ),
    )
    assert result.handoff.handoff.files[0].workspace_revision_id == second.revision_id
    assert result.handoff.handoff.files[0].workspace_revision_id != first.revision_id


async def test_note_refs_must_stay_under_human_notes() -> None:
    harness = await _scenario_harness()
    task = await harness.seed_task(
        status=TaskStatus.ASSIGNED,
        status_version=2,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=CODEX_ID,
    )
    session = await harness.open_session(task)
    with pytest.raises(NoteReferenceError):
        await harness.instruction_service.enqueue_instruction_with_notes(
            INVESTIGATION_ID,
            session.id,
            text="Read the agent note.",
            actor=HUMAN,
            idempotency_key="instruction-bad-ref",
            note_refs=("notes/agents/other/task.md",),
        )


# --------------------------------------------------------------------- D08


async def test_two_tasks_and_two_agents_never_overwrite_each_other() -> None:
    harness = await _scenario_harness()
    codex_task = await harness.seed_task(task_key="lead-codex")
    claude_task = await harness.seed_task(task_key="lead-claude")
    first = harness.workspace.add_revision(path=NOTE_PATH, sha256="8" * 64, observed_at=NOW)
    second = harness.workspace.add_revision(path=NOTE_PATH, sha256="9" * 64, observed_at=LATER)

    left, right = await asyncio.gather(
        harness.transfers.transfer(
            INVESTIGATION_ID,
            task_id=codex_task.id,
            actor=HUMAN,
            idempotency_key="transfer-d08-codex",
            request=harness.transfer_request(
                adapter_kind=OperatorAdapterKind.CODEX_CLI,
                instruction="Codex keeps the static line.",
            ),
        ),
        harness.transfers.transfer(
            INVESTIGATION_ID,
            task_id=claude_task.id,
            actor=HUMAN,
            idempotency_key="transfer-d08-claude",
            request=harness.transfer_request(
                adapter_kind=OperatorAdapterKind.CLAUDE_CODE,
                instruction="Claude takes the protocol line.",
            ),
        ),
    )

    assert left.task.id == codex_task.id
    assert right.task.id == claude_task.id
    assert left.task.assigned_actor_id == CODEX_ID
    assert right.task.assigned_actor_id == CLAUDE_ID
    assert left.task.status is TaskStatus.ASSIGNED
    assert right.task.status is TaskStatus.ASSIGNED
    stored = await harness.revisions.list_for_workspace(WORKSPACE_ID)
    assert {item.revision_id for item in stored} == {first.revision_id, second.revision_id}
    assert left.handoff.handoff.files[0].workspace_revision_id == second.revision_id
    assert right.handoff.handoff.files[0].workspace_revision_id == second.revision_id
    assert len(await harness.sessions.list_for_investigation(INVESTIGATION_ID)) == 0


# --------------------------------------------------------------- idempotency


async def test_transfer_replays_and_guards_the_expected_version() -> None:
    harness = await _scenario_harness()
    task = await harness.seed_task(status_version=3)
    harness.workspace.add_revision(path=NOTE_PATH, sha256="a" * 64)
    request = harness.transfer_request(expected_status_version=3)

    first = await harness.transfers.transfer(
        INVESTIGATION_ID,
        task_id=task.id,
        actor=HUMAN,
        idempotency_key="transfer-replay",
        request=request,
    )
    replay = await harness.transfers.transfer(
        INVESTIGATION_ID,
        task_id=task.id,
        actor=HUMAN,
        idempotency_key="transfer-replay",
        request=request,
    )
    assert replay.replayed is True
    assert replay.handoff.handoff.handoff_id == first.handoff.handoff.handoff_id
    assert replay.sync is None
    assert len(await harness.handoffs.list_for_investigation(INVESTIGATION_ID)) == 1
    assert harness.workspace.scans == 1

    other_task = await harness.seed_task(task_key="lead-other", status_version=2)
    with pytest.raises(TransferStaleVersionError):
        await harness.transfers.transfer(
            INVESTIGATION_ID,
            task_id=other_task.id,
            actor=HUMAN,
            idempotency_key="transfer-stale",
            request=harness.transfer_request(expected_status_version=1),
        )


async def test_transfer_request_shape_is_strict() -> None:
    with pytest.raises(ValidationError):
        TransferTaskRequest(to_actor_kind="operator", expected_status_version=1)
    with pytest.raises(ValidationError):
        TransferTaskRequest(
            to_actor_kind="human",
            to_adapter_kind=OperatorAdapterKind.CLAUDE_CODE,
            expected_status_version=1,
        )
    with pytest.raises(ValidationError):
        TransferTaskRequest(to_actor_kind="operator", expected_status_version=1, open_session=True)


# ------------------------------------------------- real sidecar host contract

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sidecars.workspace_agent.auth import TOKEN_FILE_NAME  # noqa: E402
from sidecars.workspace_agent.sessions import supervisor as supervisor_module  # noqa: E402
from sidecars.workspace_agent.sessions.supervisor import (  # noqa: E402
    FakeProfile,
    ProfileRegistry,
)

from cti_app.application.host_session_context import (  # noqa: E402
    CanonicalHostSessionContextProvider,
    HostSessionContextUnitOfWork,
)
from cti_app.config import Settings  # noqa: E402
from cti_app.infrastructure.agentic.wiring import build_agentic_operator_ports  # noqa: E402
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient  # noqa: E402
from tests.workspace_agent.sessions_support import (  # noqa: E402
    RunningSessionAgent,
    running_session_agent,
)


class _FakeCodexProfile(FakeProfile):  # type: ignore[misc]
    adapter_kind = "codex_cli"


class _FakeClaudeProfile(FakeProfile):  # type: ignore[misc]
    adapter_kind = "claude_code"


async def _real_ports(running: RunningSessionAgent, harness: _Harness):
    settings = Settings(
        _env_file=None,
        workspace_agent_socket=str(running.socket_path),
        workspace_agent_token_file=str(running.runtime_dir / TOKEN_FILE_NAME),
        m24_agent_session_key=SESSION_KEY,
        m24_max_contribution_bytes=4 * 1024 * 1024,
    )
    provider = CanonicalHostSessionContextProvider(
        uow_factory=cast(Callable[[], HostSessionContextUnitOfWork], lambda: harness.uow),
        settings=settings,
        clock=lambda: NOW,
    )
    ports = build_agentic_operator_ports(settings, context_provider=provider, clock=lambda: NOW)
    assert ports.available and ports.host_sessions is not None
    workspace_agent = HttpWorkspaceAgentClient(
        socket_path=settings.workspace_agent_socket,
        token_file=settings.workspace_agent_token_file,
    )
    sessions = SessionService(
        uow_factory=lambda: harness.uow,
        deriver=harness.deriver,
        operator=ports.operator,
        secret_delivery=ports.secret_delivery,
        disclosure=_allow_disclosure,
        cli_network_mode=settings.agent_sessions_cli_network_mode,
        clock=lambda: NOW,
    )
    return settings, ports, workspace_agent, sessions


async def _open_codex(
    harness: _Harness,
    sessions: SessionService,
    task: InvestigationTaskV1,
    *,
    work: HandoffWork | None = None,
    idempotency_key: str = "real-open-codex",
) -> AgentSessionV1:
    handoff = await harness.handoffs_service.create(
        INVESTIGATION_ID,
        actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
        idempotency_key=f"handoff-{idempotency_key}",
        request=HandoffCreateRequest(
            task_id=task.id,
            to_actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id=CODEX_ID),
            work=work or HandoffWork(),
            next_action="Start from the handed-off analysis.",
        ),
    )
    prepared = await sessions.open_session(
        INVESTIGATION_ID,
        task_id=task.id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        handoff_id=UUID(handoff.handoff.handoff_id),
        executable="codex-cli",
        actor=HUMAN,
        idempotency_key=idempotency_key,
    )
    return prepared.session


async def _real_sidecar_transfer(running: RunningSessionAgent, workspace_id: str) -> None:
    harness = _Harness()
    await harness.bind_workspace(workspace_id=workspace_id)
    await harness.approve_envelope()
    task = await harness.seed_task(
        status=TaskStatus.ASSIGNED,
        status_version=2,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=CODEX_ID,
    )
    _settings, ports, workspace_agent, sessions = await _real_ports(running, harness)
    transfers = InvestigationTransferService(
        uow_factory=lambda: harness.uow,
        workspace_agent=workspace_agent,
        operator=ports.operator,
        secret_delivery=ports.secret_delivery,
        deriver=harness.deriver,
        disclosure=_allow_disclosure,
        clock=lambda: NOW,
    )
    try:
        # D04: a real Codex session is interrupted by the transfer.
        codex = await _open_codex(
            harness,
            sessions,
            task,
            work=HandoffWork(
                established_results=(HandoffWorkItem(text="Static string extraction completed."),)
            ),
        )
        host_codex = await ports.host_sessions.get_session_status(codex.id)
        assert host_codex.state == "running"
        assert host_codex.adapter_kind == "codex_cli"

        result = await transfers.transfer(
            INVESTIGATION_ID,
            task_id=task.id,
            actor=HUMAN,
            idempotency_key="real-sidecar-transfer",
            request=harness.transfer_request(
                expected_status_version=harness.task_row(task.id).status_version,
                instruction="Continue the protocol check under Claude Code.",
                open_session=True,
            ),
        )
        old = await harness.sessions.get(codex.id)
        assert old is not None and old.state is AgentSessionState.CLOSED
        assert result.session is not None
        assert result.session.adapter_kind is OperatorAdapterKind.CLAUDE_CODE
        assert result.session.state is AgentSessionState.RUNNING
        assert result.handoff.handoff.version == 2
        assert [item.text for item in result.handoff.handoff.work.established_results] == [
            "Static string extraction completed."
        ]
        assert result.handoff.handoff.files[0].file_relpath == NOTE_PATH
        host_status = await ports.host_sessions.get_session_status(result.session.id)
        assert host_status.state == "running"
        assert host_status.adapter_kind == "claude_code"

        stopped = await sessions.interrupt(
            INVESTIGATION_ID,
            result.session.id,
            reason="host contract test complete",
            actor=HUMAN,
        )
        assert stopped.state is AgentSessionState.STOPPED
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = await ports.host_sessions.get_session_status(result.session.id)
            if status.state in {"stopped", "lost", "failed"}:
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - a hung fake process fails loudly
            pytest.fail("the real supervisor did not stop the fake CLI")
    finally:
        await workspace_agent.aclose()
        await ports.aclose()


async def _real_sidecar_instruction(running: RunningSessionAgent, workspace_id: str) -> None:
    from cti_app.application.workspace_agent import WorkspaceIngestService

    harness = _Harness()
    await harness.bind_workspace(workspace_id=workspace_id)
    await harness.approve_envelope()
    task = await harness.seed_task(
        status=TaskStatus.ASSIGNED,
        status_version=2,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=CODEX_ID,
    )
    _settings, ports, workspace_agent, sessions = await _real_ports(running, harness)
    ingest = WorkspaceIngestService(uow_factory=lambda: harness.uow)
    instruction_service = SessionService(
        uow_factory=lambda: harness.uow,
        deriver=harness.deriver,
        operator=ports.operator,
        secret_delivery=ports.secret_delivery,
        note_resolver=harness.notes,
        disclosure=_allow_disclosure,
        clock=lambda: NOW,
    )
    try:
        await workspace_agent.scan_workspace(
            workspace_id, full=False, idempotency_key="m24-x1-scan-a05"
        )
        await ingest.ingest_events_page(
            workspace_id=workspace_id,
            page=await workspace_agent.events(workspace_id, after=None),
        )
        await ingest.ingest_snapshot_page(
            workspace_id=workspace_id,
            page=await workspace_agent.snapshot(workspace_id, page_token=None),
        )
        first = [
            revision
            for revision in await harness.revisions.list_for_workspace(workspace_id)
            if revision.sha256 == hashlib.sha256(_NOTE_TEXT).hexdigest()
        ]
        assert len(first) == 1
        codex = await _open_codex(harness, sessions, task, idempotency_key="real-open-a05")
        enqueued = await instruction_service.enqueue_instruction_with_notes(
            INVESTIGATION_ID,
            codex.id,
            text="Read the latest human note before pivoting.",
            actor=HUMAN,
            idempotency_key="real-instruction-a05",
            note_refs=(NOTE_PATH,),
        )
        assert enqueued.queue_position == 1
        assert enqueued.delivered_live is False
        assert first[0].revision_id in enqueued.instruction.text
        assert first[0].sha256 in enqueued.instruction.text

        second_text = _NOTE_TEXT + b"\nAdditional human finding.\n"
        await workspace_agent.put_file(
            workspace_id,
            path=NOTE_PATH,
            content=second_text,
            expected_sha256=first[0].sha256,
            origin="ui",
            idempotency_key="m24-x1-note-0002",
        )
        await workspace_agent.scan_workspace(
            workspace_id, full=False, idempotency_key="m24-x1-scan-a05-2"
        )
        await ingest.ingest_snapshot_page(
            workspace_id=workspace_id,
            page=await workspace_agent.snapshot(workspace_id, page_token=None),
        )
        second = [
            revision
            for revision in await harness.revisions.list_for_workspace(workspace_id)
            if revision.sha256 == hashlib.sha256(second_text).hexdigest()
        ]
        assert len(second) == 1
        acknowledged = await instruction_service.acknowledge_instructions_with_notes(
            INVESTIGATION_ID,
            codex.id,
            instruction_ids=(enqueued.instruction.id,),
            actor=TaskActor(kind=TaskActorKind.OPERATOR, actor_id=CODEX_ID),
        )
        assert len(acknowledged) == 1
        assert acknowledged[0].note_diffs[0]["queued_revision_id"] == first[0].revision_id
        assert acknowledged[0].note_diffs[0]["current_revision_id"] == second[0].revision_id
        assert second[0].revision_id in acknowledged[0].delivery_text
        await sessions.interrupt(
            INVESTIGATION_ID, codex.id, reason="instruction test complete", actor=HUMAN
        )
    finally:
        await workspace_agent.aclose()
        await ports.aclose()


_NOTE_TEXT = b"# Note humaine\n\nThe gate is resolved on demand.\n"


def _workspace_for(running: RunningSessionAgent, client) -> str:
    ensured = client.post(
        "/v1/workspaces/ensure",
        json={
            "delivery_code": "IR202611",
            "article_slug": "loader-article",
            "subject_id": str(uuid4()),
            "investigation_id": str(INVESTIGATION_ID),
            "synthesis_sha256": "e" * 64,
        },
        headers={**running.auth_headers, "Idempotency-Key": "m24-x1-ensure-0001"},
    )
    assert ensured.status_code == 201, ensured.text
    workspace_id = ensured.json()["workspace_id"]
    assert isinstance(workspace_id, str)
    written = client.put(
        f"/v1/workspaces/{workspace_id}/files",
        params={"path": NOTE_PATH},
        content=_NOTE_TEXT,
        headers={
            **running.auth_headers,
            "X-Autowork-Expected-Sha256": "new",
            "X-Autowork-Origin": "ui",
            "Idempotency-Key": "m24-x1-note-0001",
        },
    )
    assert written.status_code in (200, 201), written.text
    return workspace_id


def _start_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supervisor_module,
        "default_profiles",
        lambda: ProfileRegistry(
            (
                _FakeCodexProfile(simulate_network_attestation=True),
                _FakeClaudeProfile(simulate_network_attestation=True),
            )
        ),
    )
    monkeypatch.setenv("AUTOWORK_FAKE_CLI_SCENARIO", "hang")
    # The fake profiles do not run bwrap, so they simulate the only supported
    # attestation (isolated_egress) instead of falling back to a host network.
    monkeypatch.delenv("AGENT_SESSIONS_CLI_NETWORK_MODE", raising=False)


def test_real_sidecar_transfer_interrupts_codex_and_opens_a_claude_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = Path(tempfile.mkdtemp(prefix="m24-x1-host-"))
    try:
        _start_sidecar(monkeypatch)
        with running_session_agent(base) as running, running.client() as client:
            workspace_id = _workspace_for(running, client)
            asyncio.run(_real_sidecar_transfer(running, workspace_id))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_real_sidecar_instruction_reports_the_modified_human_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = Path(tempfile.mkdtemp(prefix="m24-x1-instruction-"))
    try:
        _start_sidecar(monkeypatch)
        with running_session_agent(base) as running, running.client() as client:
            workspace_id = _workspace_for(running, client)
            asyncio.run(_real_sidecar_instruction(running, workspace_id))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_build_session_runtime_fails_closed_without_host_configuration() -> None:
    from cti_app.infrastructure.agentic.wiring import build_session_runtime

    unconfigured = build_session_runtime(object(), Settings(_env_file=None))
    assert unconfigured.available is False
    assert unconfigured.unavailable_reason == "workspace_agent_unconfigured"
    assert unconfigured.operator is None and unconfigured.secret_delivery is None

    class _State:
        async def uow_factory(self) -> None:  # pragma: no cover - never called
            return None

    configured = Settings(
        _env_file=None,
        workspace_agent_socket="/run/autowork/agent.sock",
        workspace_agent_token_file="/run/autowork/token",
    )
    runtime = build_session_runtime(_State(), configured)
    assert runtime.available is True
    assert runtime.unavailable_reason is None


# ----------------------------------------------------------------- API seam


async def test_transfer_route_is_human_only_and_accepts_notes_on_instruction() -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from cti_app.api.agent_sessions import router as sessions_router
    from cti_app.api.investigation_transfers import router as transfers_router
    from cti_app.application.identity import Identity, IdentityScope, LocalIdentityProvider
    from cti_app.config import Settings

    harness = await _scenario_harness()
    task = await harness.seed_task()
    harness.workspace.add_revision(path=NOTE_PATH, sha256="c" * 64)

    await harness.approve_envelope()
    app = FastAPI()
    app.include_router(transfers_router)
    app.include_router(sessions_router)
    app.state.uow_factory = lambda: harness.uow
    app.state.identity_provider = LocalIdentityProvider("analyst:test")
    app.state.settings = Settings(_env_file=None, m24_agent_session_key=SESSION_KEY)
    app.state.workspace_agent_port = harness.workspace
    app.state.agentic_operator_port = harness.operator
    app.state.agent_session_secret_delivery = harness.delivery
    app.state.investigation_composition_factory = _CompositionFactory()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        transferred = await client.post(
            f"/api/investigations/{INVESTIGATION_ID}/tasks/{task.id}/transfer",
            json={
                "to_actor_kind": "human",
                "instruction": "I take the task back with the latest note.",
                "expected_status_version": 1,
            },
            headers={"Idempotency-Key": "api-transfer-1"},
        )
        assert transferred.status_code == 200, transferred.text
        payload = transferred.json()
        assert payload["destination"]["actor_kind"] == "human"
        assert payload["handoff"]["files"][0]["file_relpath"] == NOTE_PATH
        assert payload["replayed"] is False

    assigned = await harness.seed_task(
        task_key="lead-instructions",
        status=TaskStatus.ASSIGNED,
        status_version=2,
        assigned_actor_kind=TaskActorKind.OPERATOR,
        assigned_actor_id=CODEX_ID,
    )
    session = await harness.open_session(assigned, idempotency_key="api-open")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        enqueued = await client.post(
            f"/api/investigations/{INVESTIGATION_ID}/agent-sessions/{session.id}/instructions",
            json={
                "action": "enqueue",
                "text": "Read the latest human note.",
                "note_refs": [NOTE_PATH],
            },
            headers={"Idempotency-Key": "api-instruction-1"},
        )
        assert enqueued.status_code == 200, enqueued.text
        assert enqueued.json()["queue_position"] == 1
        assert enqueued.json()["delivered_live"] is False
        assert enqueued.json()["note_diffs"] == []

    class _OperatorClient:
        async def current(self) -> Identity:
            return Identity(
                actor_id="operator:claude-client",
                scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
            )

    app.state.identity_provider = _OperatorClient()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.post(
            f"/api/investigations/{INVESTIGATION_ID}/tasks/{task.id}/transfer",
            json={
                "to_actor_kind": "operator",
                "to_adapter_kind": "claude_code",
                "expected_status_version": 1,
            },
            headers={"Idempotency-Key": "api-transfer-operator"},
        )
        assert refused.status_code == 403, refused.text

"""Oracles for the canonical host session context (M24-X1 closure 2).

The provider is the only place a prepare learns where the workspace lives, which
handoff/task/fence it starts from and what it may spend.  These tests prove it
reads canonical rows only, refuses without an approved external-model G2
envelope, never emits a host path or secret, and validates the human's model
alias.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.host_session_context import (
    CanonicalHostSessionContextProvider,
    HostSessionContextError,
    HostSessionContextForbiddenError,
    HostSessionContextUnavailableError,
    HostSessionContextUnitOfWork,
)
from cti_app.application.investigation_persistence import (
    HandoffRecord,
    InMemoryInvestigationEnvelopeRepository,
    InMemoryInvestigationHandoffRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryWorkspaceBindingRepository,
)
from cti_app.config import Settings
from cti_app.domain.agentic_operator import OperatorAdapterKind
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    InvestigationBudget,
    InvestigationEnvelope,
    TLPLevel,
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
INVESTIGATION_ID = uuid4()
TASK_ID = uuid4()
HANDOFF_ID = uuid4()
WORKSPACE_ID = "ws-" + "a" * 32


class _InvestigationRepository:
    def __init__(self, investigation_id: UUID) -> None:
        self._investigation = SimpleNamespace(id=investigation_id)

    async def get(self, investigation_id: UUID) -> Any:
        return self._investigation if investigation_id == self._investigation.id else None


class _FakeUnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.analyst_investigations = harness.investigations
        self.investigation_tasks = harness.tasks
        self.investigation_handoffs = harness.handoffs
        self.workspace_bindings = harness.bindings
        self.investigation_envelopes = harness.envelopes

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Harness:
    def __init__(self) -> None:
        self.investigations = _InvestigationRepository(INVESTIGATION_ID)
        self.tasks = InMemoryInvestigationTaskRepository(clock=lambda: NOW)
        self.handoffs = InMemoryInvestigationHandoffRepository()
        self.bindings = InMemoryWorkspaceBindingRepository()
        self.envelopes = InMemoryInvestigationEnvelopeRepository()
        self.uow = _FakeUnitOfWork(self)
        self.settings = Settings(
            _env_file=None,
            m24_max_contribution_bytes=4 * 1024 * 1024,
        )
        self.provider = CanonicalHostSessionContextProvider(
            uow_factory=cast(Callable[[], HostSessionContextUnitOfWork], lambda: self.uow),
            settings=self.settings,
            clock=lambda: NOW,
        )

    async def seed_task(
        self,
        *,
        status: TaskStatus = TaskStatus.ASSIGNED,
        lease_fence: int = 0,
        actor_kind: TaskActorKind = TaskActorKind.OPERATOR,
        capabilities: tuple[str, ...] = (),
        paths: tuple[str, ...] = ("notes/humaines", "results"),
    ) -> InvestigationTaskV1:
        task = InvestigationTaskV1(
            id=TASK_ID,
            investigation_id=INVESTIGATION_ID,
            task_key="lead-loader",
            title="Inspect the loader",
            question="Which network gate does the loader use?",
            objective="Identify the first-stage C2 gate.",
            priority=TaskPriority.HIGH,
            status=status,
            status_version=2,
            result_verification_status=ResultVerificationStatus.UNVERIFIED,
            created_by=TaskActor(kind=TaskActorKind.HUMAN, actor_id="analyst:test"),
            created_at=NOW,
            updated_at=NOW,
            required_capabilities=capabilities,
            allowed_workspace_paths=paths,
            assigned_actor_kind=actor_kind,
            assigned_actor_id=(
                "operator:codex_cli" if actor_kind is TaskActorKind.OPERATOR else "analyst:test"
            ),
            lease_fence=lease_fence,
        )
        return await self.tasks.add(task)

    async def bind_workspace(self) -> None:
        from cti_app.application.workspace_agent import WorkspaceBindingV1

        await self.bindings.add_if_absent(
            WorkspaceBindingV1(
                workspace_id=WORKSPACE_ID,
                investigation_id=INVESTIGATION_ID,
                subject_id=uuid4(),
                delivery_code="IR202611",
                article_slug="loader-article",
                synthesis_sha256="e" * 64,
                created_at=NOW,
            )
        )

    async def add_handoff(self, task: InvestigationTaskV1) -> InvestigationHandoffV1:
        handoff = InvestigationHandoffV1(
            handoff_id=str(HANDOFF_ID),
            investigation_id=str(INVESTIGATION_ID),
            task_id=str(task.id),
            version=3,
            created_at=NOW,
            from_actor=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
            to_actor=HandoffActor(kind=HandoffActorKind.OPERATOR, actor_id="operator:codex_cli"),
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
            idempotency_key="handoff-seed",
            request_sha256="d" * 64,
            created_by=HandoffActor(kind=HandoffActorKind.HUMAN, actor_id="analyst:test"),
        )
        stored, _created = await self.handoffs.append_if_absent(record)
        return stored.handoff

    async def approve_envelope(
        self,
        *,
        external_llm_allowed: bool = True,
        tlp: TLPLevel = TLPLevel.GREEN,
        tool_seconds: int | None = 1200,
        context_bytes: int | None = 8 * 1024 * 1024,
    ) -> None:
        limits: dict[BudgetDimension, int] = {}
        if tool_seconds is not None:
            limits[BudgetDimension.TOOL_SECONDS] = tool_seconds
        if context_bytes is not None:
            limits[BudgetDimension.CONTEXT_BYTES] = context_bytes
        await self.envelopes.save_if_version(
            InvestigationEnvelope(
                investigation_id=INVESTIGATION_ID,
                allowed_providers=("vt",),
                budget=InvestigationBudget(limits=limits),
                acquire_hits_allowed=False,
                disclosure=DisclosureBounds(tlp=tlp, external_llm_allowed=external_llm_allowed),
                approval_decision_id=uuid4(),
                approval_actor_id="analyst:test",
            ),
            expected_version=None,
        )

    async def context(
        self,
        *,
        adapter_kind: OperatorAdapterKind = OperatorAdapterKind.CODEX_CLI,
        requested_model: str | None = "claude-sonnet-5-5",
    ) -> Any:
        return await self.provider.host_session_context(
            session_id=uuid4(),
            investigation_id=INVESTIGATION_ID,
            task_id=TASK_ID,
            adapter_kind=adapter_kind,
            handoff_id=HANDOFF_ID,
            requested_model=requested_model,
        )


async def _ready_harness(**task_options: Any) -> tuple[_Harness, InvestigationTaskV1]:
    harness = _Harness()
    task = await harness.seed_task(**task_options)
    await harness.bind_workspace()
    await harness.add_handoff(task)
    await harness.approve_envelope()
    return harness, task


async def test_context_reads_only_canonical_records() -> None:
    harness, task = await _ready_harness(
        lease_fence=4,
        capabilities=("autowork-yara-bridge-workflow", "analysis.static", "autowork-apk-analysis"),
    )
    context = await harness.context()

    assert context.workspace_id == WORKSPACE_ID
    assert context.handoff.handoff_id == HANDOFF_ID
    assert context.handoff.version == 3
    stored = await harness.handoffs.get(HANDOFF_ID)
    assert stored is not None
    assert context.handoff.sha256 == stored.handoff.content_sha256
    assert context.lease_fence == task.lease_fence == 4
    assert context.allowed_workspace_paths == ("notes/humaines", "results")
    assert context.skill_names == ("autowork-apk-analysis", "autowork-yara-bridge-workflow")
    assert context.budgets.wall_seconds == 1200
    assert context.budgets.max_output_bytes == 4 * 1024 * 1024
    assert context.budgets.max_instructions >= 1
    assert context.model == "claude-sonnet-5-5"
    rendered = context.model_dump_json()
    assert "/home" not in rendered and "auth_secret" not in rendered


async def test_context_requires_an_approved_external_model_envelope() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    await harness.bind_workspace()
    await harness.add_handoff(task)

    with pytest.raises(HostSessionContextForbiddenError) as missing:
        await harness.context()
    assert missing.value.code == "external_model_not_allowed"
    assert missing.value.status_code == 403

    await harness.approve_envelope(external_llm_allowed=False)
    with pytest.raises(HostSessionContextForbiddenError):
        await harness.context()

    await harness.envelopes.save_if_version(
        InvestigationEnvelope(
            investigation_id=INVESTIGATION_ID,
            allowed_providers=("vt",),
            budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 1200}),
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(tlp=TLPLevel.RED, external_llm_allowed=True),
            approval_decision_id=uuid4(),
            approval_actor_id="analyst:test",
            version=2,
        ),
        expected_version=1,
    )
    with pytest.raises(HostSessionContextForbiddenError):
        await harness.context()


async def test_context_refuses_missing_workspace_binding_and_foreign_handoff() -> None:
    harness = _Harness()
    task = await harness.seed_task()
    await harness.add_handoff(task)
    await harness.approve_envelope()
    with pytest.raises(HostSessionContextUnavailableError) as missing_binding:
        await harness.context()
    assert missing_binding.value.code == "workspace_binding_missing"

    await harness.bind_workspace()
    other_id = uuid4()
    with pytest.raises(HostSessionContextError):
        await harness.provider.host_session_context(
            session_id=uuid4(),
            investigation_id=INVESTIGATION_ID,
            task_id=TASK_ID,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            handoff_id=other_id,
        )


async def test_context_refuses_human_assignment_and_invalid_model_alias() -> None:
    harness, _task = await _ready_harness(actor_kind=TaskActorKind.HUMAN)
    with pytest.raises(HostSessionContextForbiddenError):
        await harness.context()

    harness, _task = await _ready_harness()
    with pytest.raises(HostSessionContextError):
        await harness.context(requested_model="/home/analyst/bin/codex")


async def test_context_budget_never_exceeds_the_server_settings() -> None:
    harness, _task = await _ready_harness()
    await harness.envelopes.save_if_version(
        InvestigationEnvelope(
            investigation_id=INVESTIGATION_ID,
            allowed_providers=("vt",),
            budget=InvestigationBudget(
                limits={BudgetDimension.TOOL_SECONDS: 10**9, BudgetDimension.CONTEXT_BYTES: 10**9}
            ),
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(tlp=TLPLevel.GREEN, external_llm_allowed=True),
            approval_decision_id=uuid4(),
            approval_actor_id="analyst:test",
            version=2,
        ),
        expected_version=1,
    )
    context = await harness.context()
    assert context.budgets.wall_seconds == 24 * 60 * 60
    assert context.budgets.max_output_bytes == 4 * 1024 * 1024

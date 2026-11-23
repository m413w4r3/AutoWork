"""Async repository ports and in-memory fakes for investigation contracts.

The A2 persistence lot should adapt these ports to its Unit of Work. This
module intentionally does not modify or depend on application/persistence.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from cti_app.application.context_pagination import (
    KeysetPageV1,
    decode_keyset_cursor,
    decode_keyset_sort_key,
    keyset_position_after,
    make_keyset_page,
    order_keyset_items,
)
from cti_app.application.investigation_task_events import TaskEvent
from cti_app.domain.agentic_operator import (
    ACTIVE_AGENT_SESSION_STATES,
    INSTRUCTION_PENDING_STATES,
    SESSION_STATE_TRANSITIONS,
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionV1,
    InstructionState,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1, validate_analyst_opinion_rebase
from cti_app.domain.investigation import (
    DetectionCandidateVersion,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    SampleAssociation,
    ToolInvocation,
)
from cti_app.domain.investigation_experiment import InvestigationExperimentV1
from cti_app.domain.investigation_handoff import HandoffActor, InvestigationHandoffV1
from cti_app.domain.investigation_plan import (
    InvestigationPlanHead,
    InvestigationPlanVersion,
)
from cti_app.domain.investigation_task import InvestigationTaskV1, TaskStatus
from cti_app.domain.opinion_note_proposal import (
    NoteProposalStatus,
    NoteProposalV1,
)
from cti_app.domain.work_contribution import (
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionV1,
)
from cti_app.domain.workspace_note_annotation import WorkspaceNoteAnnotationV1

if TYPE_CHECKING:
    from cti_app.application.workspace_agent import (
        WorkspaceBindingV1,
        WorkspaceFileEventV1,
        WorkspaceFileRevisionV1,
        WorkspaceSyncCursorV1,
    )


class VersionConflictError(RuntimeError):
    """Raised when a compare-and-swap append does not match the current version."""


@dataclass(frozen=True, slots=True, kw_only=True)
class HandoffRecord:
    """One persisted handoff row: the domain content plus its append identity.

    ``idempotency_key``/``request_sha256`` and ``created_by`` are row metadata,
    not handoff content: the V1 payload stays exactly the D5 contract, while a
    replayed key is recognised by the canonical request hash (ADR-0011 M6).
    """

    handoff: InvestigationHandoffV1
    idempotency_key: str
    request_sha256: str
    created_by: HandoffActor
    predecessor_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, InvestigationHandoffV1):
            raise TypeError("handoff must use InvestigationHandoffV1")
        if not isinstance(self.created_by, HandoffActor):
            raise TypeError("created_by must use HandoffActor")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if len(self.request_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.request_sha256
        ):
            raise ValueError("request_sha256 must be a lowercase SHA-256")
        if self.predecessor_id is not None and not self.predecessor_id.strip():
            raise ValueError("predecessor_id must not be empty")


class ToolInvocationRepository(Protocol):
    def lock_operation_prefix(self, operation_prefix: str) -> AbstractAsyncContextManager[None]: ...

    async def get(self, invocation_id: UUID) -> ToolInvocation | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[ToolInvocation, ...]: ...

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "tool_invocations",
    ) -> KeysetPageV1[ToolInvocation]: ...

    async def get_by_operation_key(self, operation_key: str) -> ToolInvocation | None: ...

    async def add_if_absent(self, invocation: ToolInvocation) -> ToolInvocation: ...

    async def update_if_version(
        self, invocation: ToolInvocation, *, expected_version: int
    ) -> ToolInvocation: ...


class EvidenceObservationRepository(Protocol):
    async def get(self, observation_id: UUID) -> EvidenceObservation | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]: ...

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "evidence_observations",
    ) -> KeysetPageV1[EvidenceObservation]: ...

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation: ...


class FindingRepository(Protocol):
    async def get(self, finding_id: UUID) -> InvestigationFinding | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationFinding, ...]: ...

    async def version_count_for_investigation(self, investigation_id: UUID) -> int: ...

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "findings",
        latest_only: bool = False,
    ) -> KeysetPageV1[InvestigationFinding]: ...

    async def append_version(
        self, finding: InvestigationFinding, *, expected_previous_id: UUID | None
    ) -> InvestigationFinding: ...


class InvestigationExperimentRepository(Protocol):
    async def version(self, investigation_id: UUID) -> int: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationExperimentV1, ...]: ...

    async def append_if_version(
        self, experiment: InvestigationExperimentV1, *, expected_version: int
    ) -> InvestigationExperimentV1: ...


class SampleAssociationRepository(Protocol):
    async def get(self, association_id: UUID) -> SampleAssociation | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[SampleAssociation, ...]: ...

    async def append_version(
        self, association: SampleAssociation, *, expected_version: int
    ) -> SampleAssociation: ...

    async def link_sample(
        self, association_id: UUID, sample_id: UUID, *, expected_sha256: str
    ) -> None: ...


class WorkContributionRepository(Protocol):
    """Append-only contribution rows (ADR-0011 D2/D6).

    ``append_if_absent`` returns the stored row for a replay of the same
    idempotency key or of the same non-rejected workspace revision, and
    refuses a key reused for different content.  A rejected row is terminal
    and never blocks a new attempt with a new key.
    """

    async def get(self, contribution_id: UUID) -> WorkContributionV1 | None: ...

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> WorkContributionV1 | None: ...

    async def get_non_rejected_for_revision(
        self, workspace_id: str, workspace_revision_id: str
    ) -> WorkContributionV1 | None: ...

    async def list_for_investigation(
        self,
        investigation_id: UUID,
        *,
        category: WorkContributionCategory | None = None,
        import_status: WorkContributionImportStatus | None = None,
        workspace_id: str | None = None,
    ) -> tuple[WorkContributionV1, ...]: ...

    async def append_if_absent(
        self, contribution: WorkContributionV1
    ) -> tuple[WorkContributionV1, bool]: ...


class NoteProposalRepository(Protocol):
    """Non-canonical Bridge note proposals (ADR-0011 D2/D4).

    ``append_if_absent`` returns the stored row for a replay of the same
    idempotency key and refuses a key reused for a different request.  Every
    later transition is a compare-and-swap on ``status_version``.
    """

    async def get(self, proposal_id: UUID) -> NoteProposalV1 | None: ...

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> NoteProposalV1 | None: ...

    async def list_for_investigation(
        self,
        investigation_id: UUID,
        *,
        status: NoteProposalStatus | None = None,
    ) -> tuple[NoteProposalV1, ...]: ...

    async def append_if_absent(self, proposal: NoteProposalV1) -> tuple[NoteProposalV1, bool]: ...

    async def update_if_version(
        self, proposal: NoteProposalV1, *, expected_status_version: int
    ) -> NoteProposalV1 | None: ...


class WorkspaceNoteAnnotationRepository(Protocol):
    """Append-only human note annotations (ADR-0011 D6).

    ``append_if_absent`` returns the stored row for a replay of the same
    idempotency key and refuses a key reused for a different action.  No
    update or delete exists: current state is derived by ordering.
    """

    async def get(self, annotation_id: UUID) -> WorkspaceNoteAnnotationV1 | None: ...

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> WorkspaceNoteAnnotationV1 | None: ...

    async def list_for_path(
        self, investigation_id: UUID, file_relpath: str
    ) -> tuple[WorkspaceNoteAnnotationV1, ...]: ...

    async def list_for_revision(
        self, workspace_id: str, revision_id: str
    ) -> tuple[WorkspaceNoteAnnotationV1, ...]: ...

    async def append_if_absent(
        self, annotation: WorkspaceNoteAnnotationV1
    ) -> tuple[WorkspaceNoteAnnotationV1, bool]: ...


class DetectionCandidateRepository(Protocol):
    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]: ...

    async def list_for_candidate(
        self, candidate_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]: ...

    async def append_version(
        self, version: DetectionCandidateVersion, *, expected_previous_id: UUID | None
    ) -> DetectionCandidateVersion: ...

    async def append_version_idempotently(
        self, version: DetectionCandidateVersion
    ) -> tuple[DetectionCandidateVersion, bool]: ...


def _same_detection_candidate_version(
    left: DetectionCandidateVersion, right: DetectionCandidateVersion
) -> bool:
    """Compare persisted version content while ignoring its creation timestamp."""
    return (
        left.id == right.id
        and left.candidate_id == right.candidate_id
        and left.investigation_id == right.investigation_id
        and left.source == right.source
        and left.source_sha256 == right.source_sha256
        and left.motif_provenance == right.motif_provenance
        and left.provenance == right.provenance
        and left.status is right.status
        and left.version == right.version
        and left.parent_version_id == right.parent_version_id
    )


class AnalystOpinionRepository(Protocol):
    async def get(
        self, opinion_id: UUID, *, version: int | None = None
    ) -> AnalystOpinionV1 | None: ...

    async def append_version(
        self, opinion: AnalystOpinionV1, *, expected_version: int
    ) -> AnalystOpinionV1: ...

    async def append_rebased_version(
        self, opinion: AnalystOpinionV1, *, expected_version: int
    ) -> AnalystOpinionV1: ...


class InvestigationEnvelopeRepository(Protocol):
    async def get(self, investigation_id: UUID) -> InvestigationEnvelope | None: ...

    async def get_for_update(self, investigation_id: UUID) -> InvestigationEnvelope | None: ...

    async def save_if_version(
        self, envelope: InvestigationEnvelope, *, expected_version: int | None
    ) -> InvestigationEnvelope: ...


@dataclass(frozen=True, slots=True)
class InvestigationBudgetSnapshot:
    investigation_id: UUID
    version: int
    budget: InvestigationBudget

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if not isinstance(self.budget, InvestigationBudget):
            raise TypeError("budget must be an InvestigationBudget")
        object.__setattr__(self, "budget", _copy_budget(self.budget))


class InvestigationBudgetRepository(Protocol):
    async def get(self, investigation_id: UUID) -> InvestigationBudgetSnapshot | None: ...

    async def save_if_version(
        self,
        snapshot: InvestigationBudgetSnapshot,
        *,
        expected_version: int | None,
    ) -> InvestigationBudgetSnapshot: ...


class WorkspaceBindingRepository(Protocol):
    """Canonical workspace-to-investigation binding (ADR-0011 D2)."""

    async def get(self, workspace_id: str) -> WorkspaceBindingV1 | None: ...

    async def get_for_investigation(self, investigation_id: UUID) -> WorkspaceBindingV1 | None: ...

    async def add_if_absent(
        self, binding: WorkspaceBindingV1
    ) -> tuple[WorkspaceBindingV1, bool]: ...


class WorkspaceFileRevisionRepository(Protocol):
    """Per-file revision metadata; no bytes ever enter this table."""

    async def get(self, workspace_id: str, revision_id: str) -> WorkspaceFileRevisionV1 | None: ...

    async def list_for_workspace(
        self, workspace_id: str
    ) -> tuple[WorkspaceFileRevisionV1, ...]: ...

    async def append_if_absent(
        self, revision: WorkspaceFileRevisionV1
    ) -> tuple[WorkspaceFileRevisionV1, bool]: ...


class WorkspaceFileEventRepository(Protocol):
    async def get(self, workspace_id: str, event_id: str) -> WorkspaceFileEventV1 | None: ...

    async def list_for_workspace(self, workspace_id: str) -> tuple[WorkspaceFileEventV1, ...]: ...

    async def append_if_absent(
        self, event: WorkspaceFileEventV1
    ) -> tuple[WorkspaceFileEventV1, bool]: ...


class WorkspaceSyncCursorRepository(Protocol):
    async def get(self, workspace_id: str) -> WorkspaceSyncCursorV1 | None: ...

    async def save(self, cursor: WorkspaceSyncCursorV1) -> WorkspaceSyncCursorV1: ...


class InvestigationPlanRepository(Protocol):
    """Append-only plan versions plus the compare-and-swap head (ADR-0011 D1)."""

    async def get_head(self, investigation_id: UUID) -> InvestigationPlanHead | None: ...

    async def get_version(self, plan_version_id: UUID) -> InvestigationPlanVersion | None: ...

    async def get_by_version(
        self, investigation_id: UUID, version: int
    ) -> InvestigationPlanVersion | None: ...

    async def get_by_source(
        self, source_invocation_id: UUID, source_observation_id: UUID
    ) -> InvestigationPlanVersion | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationPlanVersion, ...]: ...

    async def create_version(
        self,
        version: InvestigationPlanVersion,
        *,
        expected_current_version: int | None,
    ) -> tuple[InvestigationPlanVersion, bool]: ...


class InvestigationTaskRepository(Protocol):
    """Durable tasks, append-only dependencies and the event journal (D3).

    ``lock_task`` plus ``advance_event_sequence`` make
    :func:`~cti_app.application.investigation_task_events.append_task_event`
    the sole writer of ``event_sequence``; ``update_status`` is the business
    status CAS and never writes it.  Lease primitives live here because lot
    04B2 owns the engine, not the schema.  A durable repository evaluates the
    ``renew_lease`` boundary with its own database clock; ``database_now``
    exposes that clock to the system sweep.  The in-memory fake carries an
    injectable clock instead.
    """

    async def lock_investigation(self, investigation_id: UUID) -> None: ...

    async def get(self, task_id: UUID) -> InvestigationTaskV1 | None: ...

    async def get_by_task_key(
        self, investigation_id: UUID, task_key: str
    ) -> InvestigationTaskV1 | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationTaskV1, ...]: ...

    async def list_children(self, parent_task_id: UUID) -> tuple[InvestigationTaskV1, ...]: ...

    async def list_dependencies(self, task_id: UUID) -> tuple[UUID, ...]: ...

    async def list_dependents(self, depends_on_task_id: UUID) -> tuple[UUID, ...]: ...

    async def get_event(self, task_id: UUID, idempotency_key: str) -> TaskEvent | None: ...

    async def list_events(self, task_id: UUID) -> tuple[TaskEvent, ...]: ...

    async def lock_task(self, task_id: UUID) -> InvestigationTaskV1 | None: ...

    async def add(self, task: InvestigationTaskV1) -> InvestigationTaskV1: ...

    async def add_dependency(
        self,
        *,
        investigation_id: UUID,
        task_id: UUID,
        depends_on_task_id: UUID,
    ) -> None: ...

    async def update_status(
        self,
        task: InvestigationTaskV1,
        *,
        expected_status_version: int,
        expected_status: TaskStatus,
    ) -> InvestigationTaskV1 | None: ...

    async def acquire_lease(
        self,
        task_id: UUID,
        *,
        expected_status_version: int,
        lease_id: UUID,
        lease_session_id: UUID,
        now: datetime,
        ttl_seconds: int,
    ) -> InvestigationTaskV1 | None: ...

    async def renew_lease(
        self,
        task_id: UUID,
        *,
        expected_status_version: int,
        expected_lease_fence: int,
        ttl_seconds: int,
    ) -> InvestigationTaskV1 | None: ...

    async def database_now(self) -> datetime: ...

    async def advance_event_sequence(self, task_id: UUID) -> int: ...

    async def add_event(self, event: TaskEvent) -> TaskEvent: ...


class InvestigationHandoffRepository(Protocol):
    """Append-only handoffs with per-task versions and idempotency (D5/M6).

    ``(task_id, version)`` and ``(task_id, idempotency_key)`` are unique in the
    database; the application service locks the task row before allocating a
    version, so concurrent creates of one task serialise.
    """

    async def get(self, handoff_id: UUID) -> HandoffRecord | None: ...

    async def get_by_idempotency(
        self, task_id: UUID, idempotency_key: str
    ) -> HandoffRecord | None: ...

    async def get_by_version(self, task_id: UUID, version: int) -> HandoffRecord | None: ...

    async def latest_for_task(self, task_id: UUID) -> HandoffRecord | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[HandoffRecord, ...]: ...

    async def append_if_absent(self, record: HandoffRecord) -> tuple[HandoffRecord, bool]: ...


class AgentSessionRepository(Protocol):
    """Durable technical sessions and their persisted instruction queue (D7).

    ``add`` is the partial-unique race guard: one active session per task and
    one ``(task_id, idempotency_key)`` row.  ``update_state`` is the
    ``state_version`` CAS.  ``is_active_session``/``close_session`` are the
    narrow seam lot 04B2's engine consumes as ``TaskSessionPort``.
    """

    async def get(self, session_id: UUID) -> AgentSessionV1 | None: ...

    async def get_by_idempotency(
        self, task_id: UUID, idempotency_key: str
    ) -> AgentSessionV1 | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[AgentSessionV1, ...]: ...

    async def list_for_task(self, task_id: UUID) -> tuple[AgentSessionV1, ...]: ...

    async def add(self, session: AgentSessionV1) -> tuple[AgentSessionV1, bool]: ...

    async def update_state(
        self, session: AgentSessionV1, *, expected_state_version: int
    ) -> AgentSessionV1 | None: ...

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool: ...

    async def close_session(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        outcome: str,
        reason: str,
    ) -> None: ...

    async def add_instruction(
        self, instruction: AgentSessionInstructionV1
    ) -> tuple[AgentSessionInstructionV1, bool]: ...

    async def get_instruction(self, instruction_id: UUID) -> AgentSessionInstructionV1 | None: ...

    async def get_instruction_by_idempotency(
        self, session_id: UUID, idempotency_key: str
    ) -> AgentSessionInstructionV1 | None: ...

    async def list_instructions(
        self, session_id: UUID
    ) -> tuple[AgentSessionInstructionV1, ...]: ...

    async def update_instruction(
        self,
        instruction: AgentSessionInstructionV1,
        *,
        expected_state: InstructionState,
    ) -> AgentSessionInstructionV1 | None: ...

    async def get_instruction_by_token(
        self,
        instruction_id: UUID,
        delivery_token: UUID,
        *,
        state: InstructionState | None = None,
    ) -> AgentSessionInstructionV1 | None:
        """Phase C reload: the row only if it still carries this attempt token."""
        ...

    async def list_pending_instructions(
        self, *, limit: int
    ) -> tuple[AgentSessionInstructionV1, ...]:
        """Drain candidates (queued|delivering|delivery_unknown) in (queued_at,id) order."""
        ...

    async def update_instruction_delivery(
        self,
        instruction: AgentSessionInstructionV1,
        *,
        expected_state: InstructionState,
        expected_delivery_token: UUID | None,
    ) -> AgentSessionInstructionV1:
        """CAS one delivery transition on (id, state, token); raise on a lost race."""
        ...

    async def cancel_instruction(
        self,
        instruction_id: UUID,
        *,
        expected_state: InstructionState,
        expected_delivery_token: UUID | None,
        reason: str,
        now: datetime,
    ) -> AgentSessionInstructionV1:
        """Cancel a queued/delivery_unknown instruction with its human reason."""
        ...


def _copy_budget(budget: InvestigationBudget) -> InvestigationBudget:
    copied = InvestigationBudget(limits=dict(budget.limits))
    for reservation in budget.reservations:
        # Snapshot hydration must preserve reservations admitted under an
        # earlier policy, including in-flight work retained during revocation.
        # ``reserve`` is an admission operation and would recheck current limits
        # against historical reservation amounts.
        copied.restore_reservation(reservation)
    return copied


def _memory_keyset_page[T](
    items: tuple[T, ...],
    *,
    section: str,
    investigation_id: UUID,
    cursor: str | None,
    limit: int,
    descending: bool,
    position: Callable[[T], tuple[datetime | None, UUID]],
    latest_only: bool = False,
) -> KeysetPageV1[T]:
    """Page a materialized in-memory snapshot; this does not bound source memory."""
    if not 1 <= limit <= 1_000:
        raise ValueError("Context page limit must be between 1 and 1000")
    ordered = list(order_keyset_items(items, position=position, descending=descending))
    if cursor is not None:
        decoded = decode_keyset_cursor(
            cursor,
            section=section,
            investigation_id=investigation_id,
            descending=descending,
        )
        if decoded is None:
            raise ValueError("Context keyset cursor is invalid")
        try:
            sort_key = decode_keyset_sort_key(decoded["sort_key"])
            item_id = UUID(decoded["item_id"])
        except (TypeError, ValueError):
            raise ValueError("Context keyset cursor is invalid") from None
        pivot = (sort_key, item_id)
        ordered = [
            item
            for item in ordered
            if keyset_position_after(position(item), pivot, descending=descending)
        ]
    return make_keyset_page(
        section=section,
        investigation_id=investigation_id,
        items=tuple(ordered[: limit + 1]),
        total=len(items),
        limit=limit,
        cursor=cursor,
        position=position,
        descending=descending,
        source_bounded=False,
        unbounded_reason="In-memory repository materializes the full collection",
    )


class InMemoryToolInvocationRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, ToolInvocation] = {}
        self._by_operation: dict[str, UUID] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def lock_operation_prefix(self, operation_prefix: str) -> AsyncIterator[None]:
        # The service-level asyncio lock is the in-memory implementation's
        # admission fence; there is no cross-process transaction to serialize.
        del operation_prefix
        yield

    async def get(self, invocation_id: UUID) -> ToolInvocation | None:
        return self._items.get(invocation_id)

    async def get_by_operation_key(self, operation_key: str) -> ToolInvocation | None:
        invocation_id = self._by_operation.get(operation_key)
        return self._items.get(invocation_id) if invocation_id is not None else None

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[ToolInvocation, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.investigation_id == investigation_id
                ),
                key=lambda item: (item.created_at, item.id.hex),
            )
        )

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "tool_invocations",
    ) -> KeysetPageV1[ToolInvocation]:
        items = await self.list_for_investigation(investigation_id)
        return _memory_keyset_page(
            items,
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            limit=limit,
            descending=descending,
            position=lambda item: (item.created_at, item.id),
        )

    async def add_if_absent(self, invocation: ToolInvocation) -> ToolInvocation:
        async with self._lock:
            existing_id = self._by_operation.get(invocation.operation_key)
            if existing_id is not None:
                existing = self._items[existing_id]
                if (
                    existing.investigation_id != invocation.investigation_id
                    or existing.tool_name != invocation.tool_name
                    or existing.schema_version != invocation.schema_version
                    or existing.normalized_parameters != invocation.normalized_parameters
                ):
                    raise ValueError(
                        "An operation key cannot identify different invocation content"
                    )
                return existing
            if invocation.id in self._items:
                raise ValueError("Invocation id is already used")
            self._items[invocation.id] = invocation
            self._by_operation[invocation.operation_key] = invocation.id
            return invocation

    async def update_if_version(
        self, invocation: ToolInvocation, *, expected_version: int
    ) -> ToolInvocation:
        async with self._lock:
            current = self._items.get(invocation.id)
            if current == invocation:
                return invocation
            if current is None or current.version != expected_version:
                raise VersionConflictError("Tool invocation version changed")
            if (
                invocation.operation_key != current.operation_key
                or invocation.investigation_id != current.investigation_id
                or invocation.tool_name != current.tool_name
                or invocation.schema_version != current.schema_version
                or invocation.normalized_parameters != current.normalized_parameters
                or invocation.purpose != current.purpose
                or invocation.version != expected_version + 1
            ):
                raise ValueError(
                    "Tool invocation updates must preserve identity and advance one version"
                )
            self._items[invocation.id] = invocation
            return invocation


class InMemoryEvidenceObservationRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, EvidenceObservation] = {}
        self._lock = asyncio.Lock()

    async def get(self, observation_id: UUID) -> EvidenceObservation | None:
        return self._items.get(observation_id)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return tuple(
            item for item in self._items.values() if item.investigation_id == investigation_id
        )

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "evidence_observations",
    ) -> KeysetPageV1[EvidenceObservation]:
        items = await self.list_for_investigation(investigation_id)
        return _memory_keyset_page(
            items,
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            limit=limit,
            descending=descending,
            position=lambda item: (item.observed_at, item.id),
        )

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation:
        async with self._lock:
            existing = self._items.get(observation.id)
            if existing is not None:
                if existing != observation:
                    raise ValueError(
                        "An immutable observation id cannot be reused with different content"
                    )
                return existing
            self._items[observation.id] = observation
            return observation


class InMemoryFindingRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, InvestigationFinding] = {}
        self._lock = asyncio.Lock()

    async def get(self, finding_id: UUID) -> InvestigationFinding | None:
        return self._items.get(finding_id)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationFinding, ...]:
        return tuple(
            item for item in self._items.values() if item.investigation_id == investigation_id
        )

    async def version_count_for_investigation(self, investigation_id: UUID) -> int:
        return sum(item.investigation_id == investigation_id for item in self._items.values())

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "findings",
        latest_only: bool = False,
    ) -> KeysetPageV1[InvestigationFinding]:
        items = await self.list_for_investigation(investigation_id)
        if latest_only:
            superseded = {item.supersedes for item in items if item.supersedes is not None}
            items = tuple(item for item in items if item.id not in superseded)
        return _memory_keyset_page(
            items,
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            limit=limit,
            descending=descending,
            position=lambda item: (item.created_at, item.id),
        )

    async def append_version(
        self, finding: InvestigationFinding, *, expected_previous_id: UUID | None
    ) -> InvestigationFinding:
        async with self._lock:
            if finding.id in self._items:
                existing = self._items[finding.id]
                if existing == finding:
                    return existing
                raise ValueError("Finding version id is already used")
            if finding.supersedes != expected_previous_id:
                raise ValueError("Finding supersedes must match the expected prior version")
            if expected_previous_id is None:
                if finding.version != 1:
                    raise VersionConflictError("A first finding version must be v1")
            else:
                previous = self._items.get(expected_previous_id)
                if previous is None or previous.version + 1 != finding.version:
                    raise VersionConflictError("Finding prior version changed")
                if (
                    previous.investigation_id != finding.investigation_id
                    or previous.kind is not finding.kind
                ):
                    raise ValueError("Finding versions must retain investigation and kind")
                if any(item.supersedes == expected_previous_id for item in self._items.values()):
                    raise VersionConflictError("Finding already has a newer version")
            self._items[finding.id] = finding
            return finding


class InMemorySampleAssociationRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, SampleAssociation] = {}
        self._lock = asyncio.Lock()

    async def get(self, association_id: UUID) -> SampleAssociation | None:
        return self._items.get(association_id)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        return tuple(
            item for item in self._items.values() if item.investigation_id == investigation_id
        )

    async def append_version(
        self, association: SampleAssociation, *, expected_version: int
    ) -> SampleAssociation:
        async with self._lock:
            current = self._items.get(association.id)
            if current == association:
                return association
            if current is None:
                if expected_version != 0 or association.version != 1:
                    raise VersionConflictError("A first sample association version must be v1")
            elif current.version != expected_version or association.version != expected_version + 1:
                raise VersionConflictError("Sample association version changed")
            elif (
                current.investigation_id != association.investigation_id
                or current.sample_sha256 != association.sample_sha256
                or current.origin is not association.origin
            ):
                raise ValueError("Sample association versions must retain sample identity")
            self._items[association.id] = association
            return association


class InMemoryWorkContributionRepository:
    """In-memory port fake sharing the two D2 uniqueness rules.

    One idempotency key per investigation and one non-rejected import per
    workspace revision; rows are immutable once stored.
    """

    def __init__(self) -> None:
        self._items: dict[UUID, WorkContributionV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, contribution_id: UUID) -> WorkContributionV1 | None:
        return self._items.get(contribution_id)

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> WorkContributionV1 | None:
        return next(
            (
                item
                for item in self._items.values()
                if item.investigation_id == investigation_id
                and item.idempotency_key == idempotency_key
            ),
            None,
        )

    async def get_non_rejected_for_revision(
        self, workspace_id: str, workspace_revision_id: str
    ) -> WorkContributionV1 | None:
        return next(
            (
                item
                for item in self._items.values()
                if item.workspace_id == workspace_id
                and item.workspace_revision_id == workspace_revision_id
                and item.import_status is not WorkContributionImportStatus.REJECTED
            ),
            None,
        )

    async def list_for_investigation(
        self,
        investigation_id: UUID,
        *,
        category: WorkContributionCategory | None = None,
        import_status: WorkContributionImportStatus | None = None,
        workspace_id: str | None = None,
    ) -> tuple[WorkContributionV1, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.investigation_id == investigation_id
                    and (category is None or item.category is category)
                    and (import_status is None or item.import_status is import_status)
                    and (workspace_id is None or item.workspace_id == workspace_id)
                ),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    async def append_if_absent(
        self, contribution: WorkContributionV1
    ) -> tuple[WorkContributionV1, bool]:
        async with self._lock:
            existing_key = await self.get_by_idempotency(
                contribution.investigation_id, contribution.idempotency_key
            )
            if existing_key is not None:
                if not same_work_contribution_request(existing_key, contribution):
                    raise VersionConflictError(
                        "Work contribution idempotency key records a different request"
                    )
                return existing_key, False
            if contribution.import_status is not WorkContributionImportStatus.REJECTED:
                existing_revision = await self.get_non_rejected_for_revision(
                    contribution.workspace_id, contribution.workspace_revision_id
                )
                if existing_revision is not None:
                    return existing_revision, False
            if contribution.id in self._items:
                raise VersionConflictError("Work contribution id is already used")
            self._items[contribution.id] = contribution
            return contribution, True


def same_work_contribution_request(
    stored: WorkContributionV1, candidate: WorkContributionV1
) -> bool:
    """Compare the identity a replayed idempotency key commits to."""
    return (
        stored.workspace_id == candidate.workspace_id
        and stored.workspace_revision_id == candidate.workspace_revision_id
        and stored.category is candidate.category
        and stored.origin is candidate.origin
        and stored.file_sha256 == candidate.file_sha256
        and stored.task_id == candidate.task_id
    )


def same_note_proposal_request(stored: NoteProposalV1, candidate: NoteProposalV1) -> bool:
    """Compare the identity a replayed idempotency key commits to."""
    return (
        stored.anchor == candidate.anchor
        and stored.target_block_type is candidate.target_block_type
        and stored.bridge_block_type is candidate.bridge_block_type
        and stored.consultation_type is candidate.consultation_type
        and stored.base_note_id == candidate.base_note_id
        and stored.note_state is candidate.note_state
        and stored.note_relation is candidate.note_relation
        and stored.cited_finding_refs == candidate.cited_finding_refs
        and stored.cited_observation_refs == candidate.cited_observation_refs
        and stored.base_opinion_version == candidate.base_opinion_version
        and stored.base_opinion_sha256 == candidate.base_opinion_sha256
        and stored.base_publication_artifact_id == candidate.base_publication_artifact_id
        and stored.base_publication_version == candidate.base_publication_version
        and stored.base_publication_sha256 == candidate.base_publication_sha256
        and stored.anchor_before_sha256 == candidate.anchor_before_sha256
        and stored.content_blocks == candidate.content_blocks
        and stored.task_id == candidate.task_id
    )


class InMemoryNoteProposalRepository:
    """In-memory port fake sharing the idempotency and CAS rules."""

    def __init__(self) -> None:
        self._items: dict[UUID, NoteProposalV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, proposal_id: UUID) -> NoteProposalV1 | None:
        return self._items.get(proposal_id)

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> NoteProposalV1 | None:
        return next(
            (
                item
                for item in self._items.values()
                if item.investigation_id == investigation_id
                and item.idempotency_key == idempotency_key
            ),
            None,
        )

    async def list_for_investigation(
        self,
        investigation_id: UUID,
        *,
        status: NoteProposalStatus | None = None,
    ) -> tuple[NoteProposalV1, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.investigation_id == investigation_id
                    and (status is None or item.status is status)
                ),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    async def append_if_absent(self, proposal: NoteProposalV1) -> tuple[NoteProposalV1, bool]:
        async with self._lock:
            existing_key = await self.get_by_idempotency(
                proposal.investigation_id, proposal.idempotency_key
            )
            if existing_key is not None:
                if not same_note_proposal_request(existing_key, proposal):
                    raise VersionConflictError(
                        "Note proposal idempotency key records a different request"
                    )
                return existing_key, False
            if proposal.id in self._items:
                raise VersionConflictError("Note proposal id is already used")
            self._items[proposal.id] = proposal
            return proposal, True

    async def update_if_version(
        self, proposal: NoteProposalV1, *, expected_status_version: int
    ) -> NoteProposalV1 | None:
        async with self._lock:
            current = self._items.get(proposal.id)
            if current is None or current.status_version != expected_status_version:
                return None
            if proposal.status_version != expected_status_version + 1:
                raise ValueError("A note proposal update must advance one status version")
            if current.investigation_id != proposal.investigation_id:
                raise ValueError("A note proposal update must retain its investigation")
            self._items[proposal.id] = proposal
            return proposal


def same_workspace_note_annotation_request(
    stored: WorkspaceNoteAnnotationV1, candidate: WorkspaceNoteAnnotationV1
) -> bool:
    """Compare the identity a replayed idempotency key commits to."""
    return (
        stored.workspace_id == candidate.workspace_id
        and stored.revision_id == candidate.revision_id
        and stored.file_relpath == candidate.file_relpath
        and stored.kind is candidate.kind
        and stored.actor_kind is candidate.actor_kind
        and stored.actor_id == candidate.actor_id
        and stored.target_task_id == candidate.target_task_id
        and stored.ioc_value == candidate.ioc_value
        and stored.ioc_type is candidate.ioc_type
        and stored.target_sample_id == candidate.target_sample_id
        and stored.detail == candidate.detail
    )


class InMemoryWorkspaceNoteAnnotationRepository:
    """In-memory port fake sharing the append-only and idempotency rules."""

    def __init__(self) -> None:
        self._items: dict[UUID, WorkspaceNoteAnnotationV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, annotation_id: UUID) -> WorkspaceNoteAnnotationV1 | None:
        return self._items.get(annotation_id)

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> WorkspaceNoteAnnotationV1 | None:
        return next(
            (
                item
                for item in self._items.values()
                if item.investigation_id == investigation_id
                and item.idempotency_key == idempotency_key
            ),
            None,
        )

    async def list_for_path(
        self, investigation_id: UUID, file_relpath: str
    ) -> tuple[WorkspaceNoteAnnotationV1, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.investigation_id == investigation_id
                    and item.file_relpath == file_relpath
                ),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    async def list_for_revision(
        self, workspace_id: str, revision_id: str
    ) -> tuple[WorkspaceNoteAnnotationV1, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.workspace_id == workspace_id and item.revision_id == revision_id
                ),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )

    async def append_if_absent(
        self, annotation: WorkspaceNoteAnnotationV1
    ) -> tuple[WorkspaceNoteAnnotationV1, bool]:
        async with self._lock:
            existing_key = await self.get_by_idempotency(
                annotation.investigation_id, annotation.idempotency_key
            )
            if existing_key is not None:
                if not same_workspace_note_annotation_request(existing_key, annotation):
                    raise VersionConflictError(
                        "Note annotation idempotency key records a different action"
                    )
                return existing_key, False
            if annotation.id in self._items:
                raise VersionConflictError("Note annotation id is already used")
            self._items[annotation.id] = annotation
            return annotation, True


class InMemoryDetectionCandidateRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, DetectionCandidateVersion] = {}
        self._lock = asyncio.Lock()

    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None:
        return self._items.get(version_id)

    async def list_for_candidate(self, candidate_id: UUID) -> tuple[DetectionCandidateVersion, ...]:
        return tuple(
            sorted(
                (item for item in self._items.values() if item.candidate_id == candidate_id),
                key=lambda item: (item.version, item.id.hex),
            )
        )

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.investigation_id == investigation_id
                ),
                key=lambda item: (item.candidate_id.hex, item.version, item.id.hex),
            )
        )

    async def append_version(
        self, version: DetectionCandidateVersion, *, expected_previous_id: UUID | None
    ) -> DetectionCandidateVersion:
        async with self._lock:
            if version.id in self._items:
                existing = self._items[version.id]
                if existing == version:
                    return existing
                raise ValueError("Detection version id is already used")
            if version.parent_version_id != expected_previous_id:
                raise ValueError("Detection parent must match the expected prior version")
            if expected_previous_id is None:
                if version.version != 1:
                    raise VersionConflictError("A first detection candidate must be v1")
            else:
                previous = self._items.get(expected_previous_id)
                if previous is None or previous.version + 1 != version.version:
                    raise VersionConflictError("Detection candidate prior version changed")
                if previous.candidate_id != version.candidate_id:
                    raise ValueError("Detection versions must share a candidate identity")
                if previous.investigation_id != version.investigation_id:
                    raise ValueError("Detection versions must retain investigation identity")
                if any(
                    item.parent_version_id == expected_previous_id for item in self._items.values()
                ):
                    raise VersionConflictError("Detection candidate already has a newer version")
            self._items[version.id] = version
            return version

    async def append_version_idempotently(
        self, version: DetectionCandidateVersion
    ) -> tuple[DetectionCandidateVersion, bool]:
        if version.version != 1 or version.parent_version_id is not None:
            raise ValueError("Idempotent candidate imports must create a first version")
        async with self._lock:
            existing = self._items.get(version.id)
            if existing is None:
                existing = next(
                    (
                        item
                        for item in self._items.values()
                        if item.candidate_id == version.candidate_id and item.version == 1
                    ),
                    None,
                )
            if existing is not None:
                if _same_detection_candidate_version(existing, version):
                    return existing, True
                raise VersionConflictError("Detection candidate import identity already exists")
            self._items[version.id] = version
            return version, False


class InMemoryAnalystOpinionRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, int], AnalystOpinionV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, opinion_id: UUID, *, version: int | None = None) -> AnalystOpinionV1 | None:
        if version is not None:
            return self._items.get((opinion_id, version))
        versions = [
            item for (item_id, _version), item in self._items.items() if item_id == opinion_id
        ]
        return max(versions, key=lambda item: item.version) if versions else None

    async def append_version(
        self, opinion: AnalystOpinionV1, *, expected_version: int
    ) -> AnalystOpinionV1:
        async with self._lock:
            key = (opinion.id, opinion.version)
            existing = self._items.get(key)
            if existing is not None:
                if existing == opinion:
                    return existing
                raise ValueError("Opinion version is append-only")
            current = await self.get(opinion.id)
            current_version = current.version if current else 0
            if current_version != expected_version or opinion.version != expected_version + 1:
                raise VersionConflictError("Analyst opinion version changed")
            if current is not None and (
                current.investigation_id != opinion.investigation_id
                or current.synthesis_artifact_id != opinion.synthesis_artifact_id
                or current.synthesis_sha256 != opinion.synthesis_sha256
                or current.base_publication_artifact_id != opinion.base_publication_artifact_id
                or current.base_publication_sha256 != opinion.base_publication_sha256
            ):
                raise ValueError(
                    "Opinion versions must retain their article and synthesis identity"
                )
            self._items[key] = opinion
            return opinion

    async def append_rebased_version(
        self, opinion: AnalystOpinionV1, *, expected_version: int
    ) -> AnalystOpinionV1:
        async with self._lock:
            key = (opinion.id, opinion.version)
            existing = self._items.get(key)
            if existing is not None:
                if existing == opinion:
                    return existing
                raise ValueError("Opinion version is append-only")
            current = await self.get(opinion.id)
            current_version = current.version if current else 0
            if (
                current is None
                or current_version != expected_version
                or opinion.version != expected_version + 1
            ):
                raise VersionConflictError("Analyst opinion version changed")
            validate_analyst_opinion_rebase(current, opinion)
            self._items[key] = opinion
            return opinion


class InMemoryInvestigationEnvelopeRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, InvestigationEnvelope] = {}
        self._lock = asyncio.Lock()

    async def get(self, investigation_id: UUID) -> InvestigationEnvelope | None:
        return self._items.get(investigation_id)

    async def get_for_update(self, investigation_id: UUID) -> InvestigationEnvelope | None:
        return await self.get(investigation_id)

    async def save_if_version(
        self, envelope: InvestigationEnvelope, *, expected_version: int | None
    ) -> InvestigationEnvelope:
        async with self._lock:
            current = self._items.get(envelope.investigation_id)
            if current == envelope:
                return current
            if current is None:
                if expected_version is not None or envelope.version != 1:
                    raise VersionConflictError("A first envelope version must be v1")
            elif current.version != expected_version or envelope.version != current.version + 1:
                raise VersionConflictError("Investigation envelope version changed")
            self._items[envelope.investigation_id] = envelope
            return envelope


class InMemoryInvestigationBudgetRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, InvestigationBudgetSnapshot] = {}
        self._lock = asyncio.Lock()

    async def get(self, investigation_id: UUID) -> InvestigationBudgetSnapshot | None:
        current = self._items.get(investigation_id)
        if current is None:
            return None
        return InvestigationBudgetSnapshot(
            investigation_id=current.investigation_id,
            version=current.version,
            budget=current.budget,
        )

    async def save_if_version(
        self,
        snapshot: InvestigationBudgetSnapshot,
        *,
        expected_version: int | None,
    ) -> InvestigationBudgetSnapshot:
        async with self._lock:
            current = self._items.get(snapshot.investigation_id)
            if current == snapshot:
                return InvestigationBudgetSnapshot(
                    investigation_id=current.investigation_id,
                    version=current.version,
                    budget=current.budget,
                )
            if current is None:
                if expected_version is not None or snapshot.version != 1:
                    raise VersionConflictError("A first investigation budget must be v1")
            elif current.version != expected_version or snapshot.version != current.version + 1:
                raise VersionConflictError("Investigation budget version changed")
            stored = InvestigationBudgetSnapshot(
                investigation_id=snapshot.investigation_id,
                version=snapshot.version,
                budget=snapshot.budget,
            )
            self._items[snapshot.investigation_id] = stored
            return InvestigationBudgetSnapshot(
                investigation_id=stored.investigation_id,
                version=stored.version,
                budget=stored.budget,
            )


class InMemoryWorkspaceBindingRepository:
    def __init__(self) -> None:
        self._items: dict[str, WorkspaceBindingV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, workspace_id: str) -> WorkspaceBindingV1 | None:
        return self._items.get(workspace_id)

    async def get_for_investigation(self, investigation_id: UUID) -> WorkspaceBindingV1 | None:
        return next(
            (item for item in self._items.values() if item.investigation_id == investigation_id),
            None,
        )

    async def add_if_absent(self, binding: WorkspaceBindingV1) -> tuple[WorkspaceBindingV1, bool]:
        async with self._lock:
            existing = self._items.get(binding.workspace_id)
            if existing is not None:
                if existing != binding:
                    raise ValueError(
                        "A workspace id cannot identify a different investigation binding"
                    )
                return existing, False
            if any(
                item.investigation_id == binding.investigation_id for item in self._items.values()
            ):
                raise ValueError("An investigation can bind only one workspace")
            self._items[binding.workspace_id] = binding
            return binding, True


class InMemoryWorkspaceFileRevisionRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], WorkspaceFileRevisionV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, workspace_id: str, revision_id: str) -> WorkspaceFileRevisionV1 | None:
        return self._items.get((workspace_id, revision_id))

    async def list_for_workspace(self, workspace_id: str) -> tuple[WorkspaceFileRevisionV1, ...]:
        return tuple(
            sorted(
                (
                    item
                    for (item_workspace_id, _), item in self._items.items()
                    if item_workspace_id == workspace_id
                ),
                key=lambda item: (item.path, item.observed_at, item.revision_id),
            )
        )

    async def append_if_absent(
        self, revision: WorkspaceFileRevisionV1
    ) -> tuple[WorkspaceFileRevisionV1, bool]:
        async with self._lock:
            existing = self._items.get((revision.workspace_id, revision.revision_id))
            if existing is not None:
                if existing != revision:
                    raise ValueError("A revision id cannot identify a different workspace file")
                return existing, False
            self._items[(revision.workspace_id, revision.revision_id)] = revision
            return revision, True


class InMemoryWorkspaceFileEventRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], WorkspaceFileEventV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, workspace_id: str, event_id: str) -> WorkspaceFileEventV1 | None:
        return self._items.get((workspace_id, event_id))

    async def list_for_workspace(self, workspace_id: str) -> tuple[WorkspaceFileEventV1, ...]:
        return tuple(
            sorted(
                (
                    item
                    for (item_workspace_id, _), item in self._items.items()
                    if item_workspace_id == workspace_id
                ),
                key=lambda item: (item.generation, item.seq, item.event_id),
            )
        )

    async def append_if_absent(
        self, event: WorkspaceFileEventV1
    ) -> tuple[WorkspaceFileEventV1, bool]:
        async with self._lock:
            existing = self._items.get((event.workspace_id, event.event_id))
            if existing is not None:
                if existing != event:
                    raise ValueError("An event id cannot identify different event content")
                return existing, False
            if any(
                item.workspace_id == event.workspace_id
                and item.generation == event.generation
                and item.seq == event.seq
                for item in self._items.values()
            ):
                raise ValueError("An event sequence cannot identify a different event id")
            self._items[(event.workspace_id, event.event_id)] = event
            return event, True


class InMemoryWorkspaceSyncCursorRepository:
    def __init__(self) -> None:
        self._items: dict[str, WorkspaceSyncCursorV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, workspace_id: str) -> WorkspaceSyncCursorV1 | None:
        return self._items.get(workspace_id)

    async def save(self, cursor: WorkspaceSyncCursorV1) -> WorkspaceSyncCursorV1:
        async with self._lock:
            current = self._items.get(cursor.workspace_id)
            if current is not None and (
                current.generation == cursor.generation and current.cursor >= cursor.cursor
            ):
                return current
            self._items[cursor.workspace_id] = cursor
            return cursor


class InMemoryInvestigationPlanRepository:
    def __init__(self) -> None:
        self._heads: dict[UUID, InvestigationPlanHead] = {}
        self._items: dict[UUID, InvestigationPlanVersion] = {}
        self._lock = asyncio.Lock()

    async def get_head(self, investigation_id: UUID) -> InvestigationPlanHead | None:
        return self._heads.get(investigation_id)

    async def get_version(self, plan_version_id: UUID) -> InvestigationPlanVersion | None:
        return self._items.get(plan_version_id)

    async def get_by_version(
        self, investigation_id: UUID, version: int
    ) -> InvestigationPlanVersion | None:
        return next(
            (
                item
                for item in self._items.values()
                if item.investigation_id == investigation_id and item.version == version
            ),
            None,
        )

    async def get_by_source(
        self, source_invocation_id: UUID, source_observation_id: UUID
    ) -> InvestigationPlanVersion | None:
        return next(
            (
                item
                for item in self._items.values()
                if item.source_invocation_id == source_invocation_id
                and item.source_observation_id == source_observation_id
            ),
            None,
        )

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationPlanVersion, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.investigation_id == investigation_id
                ),
                key=lambda item: item.version,
            )
        )

    async def create_version(
        self,
        version: InvestigationPlanVersion,
        *,
        expected_current_version: int | None,
    ) -> tuple[InvestigationPlanVersion, bool]:
        async with self._lock:
            if any(
                item.investigation_id == version.investigation_id
                and item.version == version.version
                for item in self._items.values()
            ):
                raise VersionConflictError("Plan version already exists")
            if any(
                item.source_invocation_id == version.source_invocation_id
                and item.source_observation_id == version.source_observation_id
                for item in self._items.values()
            ):
                raise VersionConflictError("Plan version source already produced a version")
            head = self._heads.get(version.investigation_id)
            if expected_current_version is None:
                if head is not None or version.version != 1:
                    raise VersionConflictError("A first plan version must be v1")
                self._heads[version.investigation_id] = InvestigationPlanHead(
                    investigation_id=version.investigation_id,
                    current_version=1,
                    created_at=version.created_at,
                    updated_at=version.created_at,
                )
            else:
                if (
                    head is None
                    or head.current_version != expected_current_version
                    or version.version != expected_current_version + 1
                ):
                    raise VersionConflictError("Plan current version changed")
                self._heads[version.investigation_id] = InvestigationPlanHead(
                    investigation_id=version.investigation_id,
                    current_version=version.version,
                    created_at=head.created_at,
                    updated_at=version.created_at,
                )
            self._items[version.id] = version
            return version, True


class InMemoryInvestigationTaskRepository:
    """In-memory port fake sharing the SQL repository's observable rules.

    ``clock`` stands in for PostgreSQL's ``statement_timestamp()``: the fake
    has no database, so tests inject the time the lease predicates see.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._tasks: dict[UUID, InvestigationTaskV1] = {}
        self._dependencies: set[tuple[UUID, UUID]] = set()
        self._events: list[TaskEvent] = []
        self._lock = asyncio.Lock()
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    async def lock_investigation(self, investigation_id: UUID) -> None:
        return None

    async def get(self, task_id: UUID) -> InvestigationTaskV1 | None:
        return self._tasks.get(task_id)

    async def get_by_task_key(
        self, investigation_id: UUID, task_key: str
    ) -> InvestigationTaskV1 | None:
        return next(
            (
                task
                for task in self._tasks.values()
                if task.investigation_id == investigation_id and task.task_key == task_key
            ),
            None,
        )

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationTaskV1, ...]:
        return tuple(
            sorted(
                (
                    task
                    for task in self._tasks.values()
                    if task.investigation_id == investigation_id
                ),
                key=lambda task: (task.created_at, str(task.id)),
            )
        )

    async def list_children(self, parent_task_id: UUID) -> tuple[InvestigationTaskV1, ...]:
        return tuple(
            sorted(
                (task for task in self._tasks.values() if task.parent_task_id == parent_task_id),
                key=lambda task: (task.created_at, str(task.id)),
            )
        )

    async def list_dependencies(self, task_id: UUID) -> tuple[UUID, ...]:
        return tuple(
            sorted(depends_on for task, depends_on in self._dependencies if task == task_id)
        )

    async def list_dependents(self, depends_on_task_id: UUID) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                task for task, depends_on in self._dependencies if depends_on == depends_on_task_id
            )
        )

    async def get_event(self, task_id: UUID, idempotency_key: str) -> TaskEvent | None:
        return next(
            (
                event
                for event in self._events
                if event.task_id == task_id and event.idempotency_key == idempotency_key
            ),
            None,
        )

    async def list_events(self, task_id: UUID) -> tuple[TaskEvent, ...]:
        return tuple(
            sorted(
                (event for event in self._events if event.task_id == task_id),
                key=lambda event: event.sequence,
            )
        )

    async def lock_task(self, task_id: UUID) -> InvestigationTaskV1 | None:
        return self._tasks.get(task_id)

    async def add(self, task: InvestigationTaskV1) -> InvestigationTaskV1:
        async with self._lock:
            existing = await self.get_by_task_key(task.investigation_id, task.task_key)
            if existing is not None:
                if existing != task:
                    raise VersionConflictError("Task key already materialised a different task")
                return existing
            if task.id in self._tasks:
                raise VersionConflictError("Task id is already used")
            self._tasks[task.id] = task
            return task

    async def add_dependency(
        self,
        *,
        investigation_id: UUID,
        task_id: UUID,
        depends_on_task_id: UUID,
    ) -> None:
        if task_id == depends_on_task_id:
            raise ValueError("A task cannot depend on itself")
        task = self._tasks.get(task_id)
        dependency = self._tasks.get(depends_on_task_id)
        if (
            task is None
            or task.investigation_id != investigation_id
            or dependency is None
            or dependency.investigation_id != investigation_id
        ):
            raise VersionConflictError("A dependency must join two tasks of one investigation")
        async with self._lock:
            self._dependencies.add((task_id, depends_on_task_id))

    async def update_status(
        self,
        task: InvestigationTaskV1,
        *,
        expected_status_version: int,
        expected_status: TaskStatus,
    ) -> InvestigationTaskV1 | None:
        if task.status_version != expected_status_version + 1:
            raise ValueError("A status change increments status_version exactly once")
        if task.status is expected_status:
            raise ValueError("A status change must change the status")
        async with self._lock:
            current = self._tasks.get(task.id)
            if (
                current is None
                or current.status_version != expected_status_version
                or current.status is not expected_status
            ):
                return None
            self._tasks[task.id] = task
            return task

    async def acquire_lease(
        self,
        task_id: UUID,
        *,
        expected_status_version: int,
        lease_id: UUID,
        lease_session_id: UUID,
        now: datetime,
        ttl_seconds: int,
    ) -> InvestigationTaskV1 | None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        async with self._lock:
            current = self._tasks.get(task_id)
            if (
                current is None
                or current.status_version != expected_status_version
                or current.status is not TaskStatus.ASSIGNED
            ):
                return None
            updated = replace(
                current,
                status=TaskStatus.RUNNING,
                status_version=current.status_version + 1,
                lease_id=lease_id,
                lease_session_id=lease_session_id,
                lease_fence=current.lease_fence + 1,
                lease_expires_at=now + timedelta(seconds=ttl_seconds),
                heartbeat_at=now,
                updated_at=now,
            )
            self._tasks[task_id] = updated
            return updated

    async def renew_lease(
        self,
        task_id: UUID,
        *,
        expected_status_version: int,
        expected_lease_fence: int,
        ttl_seconds: int,
    ) -> InvestigationTaskV1 | None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        now = self._clock()
        async with self._lock:
            current = self._tasks.get(task_id)
            if (
                current is None
                or current.status not in (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT)
                or current.status_version != expected_status_version
                or current.lease_fence != expected_lease_fence
                or current.lease_expires_at is None
                or current.lease_expires_at <= now
            ):
                return None
            updated = replace(
                current,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=ttl_seconds),
                updated_at=now,
            )
            self._tasks[task_id] = updated
            return updated

    async def database_now(self) -> datetime:
        return self._clock()

    async def advance_event_sequence(self, task_id: UUID) -> int:
        async with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise VersionConflictError("Task disappeared before event append")
            updated = replace(current, event_sequence=current.event_sequence + 1)
            self._tasks[task_id] = updated
            return updated.event_sequence

    async def add_event(self, event: TaskEvent) -> TaskEvent:
        async with self._lock:
            if any(
                existing.task_id == event.task_id
                and (
                    existing.sequence == event.sequence
                    or existing.idempotency_key == event.idempotency_key
                )
                for existing in self._events
            ):
                raise VersionConflictError("Task event sequence or idempotency key is already used")
            self._events.append(event)
            return event


class InMemoryInvestigationHandoffRepository:
    """In-memory port fake sharing the SQL repository's observable rules."""

    def __init__(self) -> None:
        self._items: dict[UUID, HandoffRecord] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _handoff_id(record: HandoffRecord) -> UUID:
        return UUID(record.handoff.handoff_id)

    async def get(self, handoff_id: UUID) -> HandoffRecord | None:
        return self._items.get(handoff_id)

    async def get_by_idempotency(self, task_id: UUID, idempotency_key: str) -> HandoffRecord | None:
        return next(
            (
                record
                for record in self._items.values()
                if record.handoff.task_id == str(task_id)
                and record.idempotency_key == idempotency_key
            ),
            None,
        )

    async def get_by_version(self, task_id: UUID, version: int) -> HandoffRecord | None:
        return next(
            (
                record
                for record in self._items.values()
                if record.handoff.task_id == str(task_id) and record.handoff.version == version
            ),
            None,
        )

    async def latest_for_task(self, task_id: UUID) -> HandoffRecord | None:
        matching = [
            record for record in self._items.values() if record.handoff.task_id == str(task_id)
        ]
        if not matching:
            return None
        return max(matching, key=lambda record: record.handoff.version)

    async def list_for_investigation(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[HandoffRecord, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in self._items.values()
                    if record.handoff.investigation_id == str(investigation_id)
                    and (task_id is None or record.handoff.task_id == str(task_id))
                ),
                key=lambda record: (
                    record.handoff.created_at,
                    record.handoff.version,
                    record.handoff.handoff_id,
                ),
            )
        )

    async def append_if_absent(self, record: HandoffRecord) -> tuple[HandoffRecord, bool]:
        async with self._lock:
            for existing in self._items.values():
                if existing.handoff.task_id != record.handoff.task_id:
                    continue
                if (
                    existing.handoff.version == record.handoff.version
                    or existing.idempotency_key == record.idempotency_key
                ):
                    raise VersionConflictError(
                        "Handoff task version or idempotency key is already used"
                    )
            handoff_id = self._handoff_id(record)
            if handoff_id in self._items:
                raise VersionConflictError("Handoff id is already used")
            self._items[handoff_id] = record
            return record, True


class InMemoryAgentSessionRepository:
    """In-memory port fake sharing the SQL repository's observable rules.

    It enforces the two D2 uniqueness rules (one active session per task, one
    idempotency key per task) and the instruction queue's idempotency, so the
    service tests race the same invariants the database enforces.
    """

    def __init__(self) -> None:
        self._sessions: dict[UUID, AgentSessionV1] = {}
        self._instructions: dict[UUID, AgentSessionInstructionV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, session_id: UUID) -> AgentSessionV1 | None:
        return self._sessions.get(session_id)

    async def get_by_idempotency(
        self, task_id: UUID, idempotency_key: str
    ) -> AgentSessionV1 | None:
        return next(
            (
                session
                for session in self._sessions.values()
                if session.task_id == task_id and session.idempotency_key == idempotency_key
            ),
            None,
        )

    async def list_for_investigation(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[AgentSessionV1, ...]:
        return tuple(
            sorted(
                (
                    session
                    for session in self._sessions.values()
                    if session.investigation_id == investigation_id
                    and (task_id is None or session.task_id == task_id)
                ),
                key=lambda session: (session.created_at, str(session.id)),
            )
        )

    async def list_for_task(self, task_id: UUID) -> tuple[AgentSessionV1, ...]:
        return tuple(
            sorted(
                (session for session in self._sessions.values() if session.task_id == task_id),
                key=lambda session: (session.created_at, str(session.id)),
            )
        )

    async def add(self, session: AgentSessionV1) -> tuple[AgentSessionV1, bool]:
        async with self._lock:
            for existing in self._sessions.values():
                if (
                    existing.task_id == session.task_id
                    and existing.idempotency_key == session.idempotency_key
                ):
                    if (
                        existing.adapter_kind is not session.adapter_kind
                        or existing.handoff_id != session.handoff_id
                        or existing.capability_sha256 != session.capability_sha256
                    ):
                        raise VersionConflictError(
                            "Agent session idempotency key records a different request"
                        )
                    return existing, False
                if existing.task_id == session.task_id and existing.is_active and session.is_active:
                    raise VersionConflictError("The task already has an active agent session")
                if (
                    session.host_session_ref is not None
                    and existing.host_session_ref == session.host_session_ref
                ):
                    raise VersionConflictError("The host session reference is already used")
            if session.id in self._sessions:
                raise VersionConflictError("Agent session id is already used")
            self._sessions[session.id] = session
            return session, True

    async def update_state(
        self, session: AgentSessionV1, *, expected_state_version: int
    ) -> AgentSessionV1 | None:
        if session.state_version != expected_state_version + 1:
            raise ValueError("A session update increments state_version exactly once")
        async with self._lock:
            current = self._sessions.get(session.id)
            if current is None or current.state_version != expected_state_version:
                return None
            self._sessions[session.id] = session
            return session

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        session = self._sessions.get(session_id)
        return (
            session is not None
            and session.investigation_id == investigation_id
            and session.task_id == task_id
            and session.state in ACTIVE_AGENT_SESSION_STATES
        )

    async def close_session(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        outcome: str,
        reason: str,
    ) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if (
                session is None
                or session.investigation_id != investigation_id
                or session.task_id != task_id
            ):
                raise VersionConflictError("The task lease names no bound agent session")
            if session.is_terminal or session.state is AgentSessionState.LOST:
                return
            target = AgentSessionState.CLOSED if outcome == "closed" else AgentSessionState.LOST
            if target not in SESSION_STATE_TRANSITIONS[session.state]:
                self._sessions[session_id] = replace(
                    session,
                    state=AgentSessionState.LOST,
                    state_version=session.state_version + 1,
                    close_reason=reason,
                    updated_at=session.updated_at,
                )
                return
            self._sessions[session_id] = session.with_state(
                target, now=session.updated_at, close_reason=reason
            )

    async def add_instruction(
        self, instruction: AgentSessionInstructionV1
    ) -> tuple[AgentSessionInstructionV1, bool]:
        async with self._lock:
            for existing in self._instructions.values():
                if (
                    existing.session_id == instruction.session_id
                    and existing.idempotency_key == instruction.idempotency_key
                ):
                    if existing.text_sha256 != instruction.text_sha256:
                        raise VersionConflictError(
                            "Instruction idempotency key records a different body"
                        )
                    return existing, False
            if instruction.id in self._instructions:
                raise VersionConflictError("Instruction id is already used")
            self._instructions[instruction.id] = instruction
            return instruction, True

    async def get_instruction(self, instruction_id: UUID) -> AgentSessionInstructionV1 | None:
        return self._instructions.get(instruction_id)

    async def get_instruction_by_idempotency(
        self, session_id: UUID, idempotency_key: str
    ) -> AgentSessionInstructionV1 | None:
        return next(
            (
                instruction
                for instruction in self._instructions.values()
                if instruction.session_id == session_id
                and instruction.idempotency_key == idempotency_key
            ),
            None,
        )

    async def list_instructions(self, session_id: UUID) -> tuple[AgentSessionInstructionV1, ...]:
        return tuple(
            sorted(
                (
                    instruction
                    for instruction in self._instructions.values()
                    if instruction.session_id == session_id
                ),
                key=lambda instruction: (instruction.queued_at, str(instruction.id)),
            )
        )

    async def update_instruction(
        self,
        instruction: AgentSessionInstructionV1,
        *,
        expected_state: InstructionState,
    ) -> AgentSessionInstructionV1 | None:
        async with self._lock:
            current = self._instructions.get(instruction.id)
            if current is None or current.state is not expected_state:
                return None
            self._instructions[instruction.id] = instruction
            return instruction

    async def get_instruction_by_token(
        self,
        instruction_id: UUID,
        delivery_token: UUID,
        *,
        state: InstructionState | None = None,
    ) -> AgentSessionInstructionV1 | None:
        current = self._instructions.get(instruction_id)
        if current is None or current.delivery_token != delivery_token:
            return None
        if state is not None and current.state is not state:
            return None
        return current

    async def list_pending_instructions(
        self, *, limit: int
    ) -> tuple[AgentSessionInstructionV1, ...]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        pending = sorted(
            (
                instruction
                for instruction in self._instructions.values()
                if instruction.state in INSTRUCTION_PENDING_STATES
            ),
            key=lambda instruction: (instruction.queued_at, str(instruction.id)),
        )
        return tuple(pending[:limit])

    async def update_instruction_delivery(
        self,
        instruction: AgentSessionInstructionV1,
        *,
        expected_state: InstructionState,
        expected_delivery_token: UUID | None,
    ) -> AgentSessionInstructionV1:
        async with self._lock:
            current = self._instructions.get(instruction.id)
            if (
                current is None
                or current.state is not expected_state
                or current.delivery_token != expected_delivery_token
            ):
                raise VersionConflictError("The instruction delivery attempt changed concurrently")
            self._instructions[instruction.id] = instruction
            return instruction

    async def cancel_instruction(
        self,
        instruction_id: UUID,
        *,
        expected_state: InstructionState,
        expected_delivery_token: UUID | None,
        reason: str,
        now: datetime,
    ) -> AgentSessionInstructionV1:
        async with self._lock:
            current = self._instructions.get(instruction_id)
            if (
                current is None
                or current.state is not expected_state
                or current.delivery_token != expected_delivery_token
            ):
                raise VersionConflictError("The instruction delivery attempt changed concurrently")
            cancelled = current.with_cancellation(reason=reason, now=now)
            self._instructions[instruction_id] = cancelled
            return cancelled


__all__ = [
    "AgentSessionRepository",
    "AnalystOpinionRepository",
    "DetectionCandidateRepository",
    "EvidenceObservationRepository",
    "FindingRepository",
    "HandoffRecord",
    "InMemoryAgentSessionRepository",
    "InMemoryAnalystOpinionRepository",
    "InMemoryDetectionCandidateRepository",
    "InMemoryEvidenceObservationRepository",
    "InMemoryFindingRepository",
    "InMemoryInvestigationBudgetRepository",
    "InMemoryInvestigationEnvelopeRepository",
    "InMemoryInvestigationHandoffRepository",
    "InMemoryInvestigationPlanRepository",
    "InMemoryInvestigationTaskRepository",
    "InMemoryNoteProposalRepository",
    "InMemorySampleAssociationRepository",
    "InMemoryToolInvocationRepository",
    "InMemoryWorkContributionRepository",
    "InMemoryWorkspaceBindingRepository",
    "InMemoryWorkspaceFileEventRepository",
    "InMemoryWorkspaceFileRevisionRepository",
    "InMemoryWorkspaceNoteAnnotationRepository",
    "InMemoryWorkspaceSyncCursorRepository",
    "InvestigationBudgetRepository",
    "InvestigationBudgetSnapshot",
    "InvestigationEnvelopeRepository",
    "InvestigationHandoffRepository",
    "InvestigationPlanRepository",
    "InvestigationTaskRepository",
    "NoteProposalRepository",
    "SampleAssociationRepository",
    "ToolInvocationRepository",
    "VersionConflictError",
    "WorkContributionRepository",
    "WorkspaceBindingRepository",
    "WorkspaceFileEventRepository",
    "WorkspaceFileRevisionRepository",
    "WorkspaceNoteAnnotationRepository",
    "WorkspaceSyncCursorRepository",
    "same_note_proposal_request",
    "same_work_contribution_request",
    "same_workspace_note_annotation_request",
]

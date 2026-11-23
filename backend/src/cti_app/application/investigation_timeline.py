"""Read-only investigation timeline projection (M24-04B3, ADR-0011 D8/P3).

The timeline is a view, never a second journal.  It unions the canonical
records that already exist — task events, analyst decisions, evidence
observations, tool invocations and plan versions — orders them by
``(occurred_at, id)`` and pages with a keyset cursor.

Deduplication (P3):

* a task event that references an ``analyst_decision_id`` is absorbed by that
  decision item (the decision wins, the event is nested through
  ``linked_event_ids``);
* a Bridge consultation (``tool_name='advisor.consult'``) and the advisory
  observations attached to its invocation form one ``bridge_consultation``
  item keyed by the invocation; the observations are linked, not repeated.

Every item carries ``item_type``, ``object_id``, ``task_id``, ``actor_kind``,
``actor_id``, ``occurred_at``, a terse ``summary`` (ids, hashes and short
status words only, never analyst prose), navigation ``refs`` and an
``evidence`` boolean.  Only canonical observations are ``evidence: true``;
Bridge advice, plans, invocations and events are ``evidence: false``.

Handoffs (lot 05A), work contributions (lot 07), agent sessions and their
persisted human instruction queue (lots 05B1/D05) plug in through
:class:`TimelineSource` without touching the union below.  They are
append-only projections and never evidence: a contribution that imported an
observation leaves the canonical observation as its own ``observation`` item.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    EvidenceObservationRepository,
    HandoffRecord,
    InvestigationHandoffRepository,
    InvestigationPlanRepository,
    InvestigationTaskRepository,
    ToolInvocationRepository,
    WorkContributionRepository,
)
from cti_app.application.investigation_task_events import TaskEvent
from cti_app.application.persistence import AnalystDecisionRepository
from cti_app.domain.agentic_operator import (
    AgentSessionInstructionV1,
    AgentSessionV1,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
)
from cti_app.domain.investigation import (
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationActorKind,
)
from cti_app.domain.investigation_plan import InvestigationPlanVersion
from cti_app.domain.investigation_task import TaskActorKind
from cti_app.domain.work_contribution import (
    WorkContributionOrigin,
    WorkContributionV1,
)

_TIMELINE_SECTION = "timeline"
_ADVISOR_TOOL_NAME = "advisor.consult"


class TimelineItemType(StrEnum):
    """The union's item kinds; each maps to one canonical authority."""

    TASK_EVENT = "task_event"
    DECISION = "decision"
    OBSERVATION = "observation"
    BRIDGE_CONSULTATION = "bridge_consultation"
    TOOL_INVOCATION = "tool_invocation"
    PLAN_VERSION = "plan_version"
    HANDOFF = "handoff"
    CONTRIBUTION = "contribution"
    AGENT_SESSION = "agent_session"
    INSTRUCTION = "instruction"


class TimelineActorKind(StrEnum):
    """Who produced the record; ``source`` is an unattributed producer."""

    HUMAN = "human"
    OPERATOR = "operator"
    SYSTEM = "system"
    SOURCE = "source"


class TimelineItemV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    item_type: TimelineItemType
    object_id: UUID
    occurred_at: datetime
    task_id: UUID | None = None
    actor_kind: TimelineActorKind
    actor_id: str | None = None
    summary: str = Field(min_length=1, max_length=200)
    evidence: bool
    refs: dict[str, str] = Field(default_factory=dict)
    linked_event_ids: tuple[UUID, ...] = ()
    linked_observation_ids: tuple[UUID, ...] = ()


class TimelinePageV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    items: tuple[TimelineItemV1, ...]
    next_cursor: str | None = None
    has_more: bool


class TimelineSource(Protocol):
    """A future canonical source (handoffs in 05A, contributions in 07)."""

    async def items(self, investigation_id: UUID) -> Sequence[TimelineItemV1]: ...


def _encode_cursor(payload: dict[str, str]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def encode_position_cursor(*, investigation_id: UUID, occurred_at: datetime, item_id: UUID) -> str:
    """Encode the ``(occurred_at, id)`` keyset position of one item."""
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return _encode_cursor(
        {
            "v": "1",
            "kind": _TIMELINE_SECTION,
            "investigation_id": str(investigation_id),
            "occurred_at": occurred_at.isoformat(),
            "item_id": str(item_id),
        }
    )


def decode_position_cursor(
    cursor: str | None, *, investigation_id: UUID
) -> tuple[datetime, UUID] | None:
    """Decode a position cursor bound to this investigation; ``None`` if invalid."""
    if cursor is None:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "kind",
        "investigation_id",
        "occurred_at",
        "item_id",
    }:
        return None
    if (
        payload["v"] != "1"
        or payload["kind"] != _TIMELINE_SECTION
        or payload["investigation_id"] != str(investigation_id)
    ):
        return None
    try:
        occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
        item_id = UUID(str(payload["item_id"]))
    except (TypeError, ValueError):
        return None
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        return None
    return occurred_at, item_id


def _is_advisory_observation(observation: EvidenceObservation) -> bool:
    """Consultative Bridge material is never evidence (ADR-0011 D6/B7).

    Lot 07 adds the canonical ``is_advisory_observation`` helper to
    ``domain/investigation.py``; until then the timeline applies the same
    closed predicate locally so advisory items are already labelled.
    """
    return observation.source.startswith("advisor.") or observation.object_type.startswith(
        "advisor_"
    )


def _actor_kind(kind: TaskActorKind) -> TimelineActorKind:
    return TimelineActorKind(kind.value)


def _invocation_actor(
    invocation: ToolInvocation,
) -> tuple[TimelineActorKind, str | None]:
    if invocation.actor_kind is ToolInvocationActorKind.HUMAN:
        return TimelineActorKind.HUMAN, invocation.actor_id
    if invocation.actor_kind is ToolInvocationActorKind.OPERATOR:
        return TimelineActorKind.OPERATOR, invocation.actor_id
    return TimelineActorKind.SOURCE, invocation.tool_name


def _task_event_item(event: TaskEvent) -> TimelineItemV1:
    from_status = event.from_status.value if event.from_status is not None else "-"
    to_status = event.to_status.value if event.to_status is not None else "-"
    return TimelineItemV1(
        item_type=TimelineItemType.TASK_EVENT,
        object_id=event.id,
        occurred_at=event.occurred_at,
        task_id=event.task_id,
        actor_kind=_actor_kind(event.actor.kind),
        actor_id=event.actor.actor_id,
        summary=f"{event.event_type.value}:{from_status}->{to_status}",
        evidence=False,
        refs={"task_id": str(event.task_id), "event_id": str(event.id)},
    )


def _decision_item(decision: AnalystDecision, events: Sequence[TaskEvent]) -> TimelineItemV1:
    targeted_task = (
        decision.target_id if decision.target_type is AnalystDecisionTargetType.TASK else None
    )
    refs = {
        "decision_id": str(decision.id),
        "target_type": decision.target_type.value,
        "target_id": str(decision.target_id),
    }
    if targeted_task is not None:
        refs["task_id"] = str(targeted_task)
    return TimelineItemV1(
        item_type=TimelineItemType.DECISION,
        object_id=decision.id,
        occurred_at=decision.occurred_at,
        task_id=targeted_task,
        actor_kind=TimelineActorKind.HUMAN,
        actor_id=decision.actor_id,
        summary=f"{decision.decision_type.value}:{decision.target_type.value}",
        evidence=False,
        refs=refs,
        linked_event_ids=tuple(event.id for event in events),
    )


def _observation_item(observation: EvidenceObservation) -> TimelineItemV1:
    advisory = _is_advisory_observation(observation)
    return TimelineItemV1(
        item_type=TimelineItemType.OBSERVATION,
        object_id=observation.id,
        occurred_at=observation.observed_at,
        task_id=None,
        actor_kind=TimelineActorKind.SOURCE,
        actor_id=observation.source,
        summary=f"{observation.kind.value}:{observation.object_type}",
        evidence=not advisory,
        refs={
            "observation_id": str(observation.id),
            "blob_id": str(observation.blob_id),
            "sha256": observation.sha256,
        },
    )


def _consultation_item(
    invocation: ToolInvocation, observations: Sequence[EvidenceObservation]
) -> TimelineItemV1:
    actor_kind, actor_id = _invocation_actor(invocation)
    return TimelineItemV1(
        item_type=TimelineItemType.BRIDGE_CONSULTATION,
        object_id=invocation.id,
        occurred_at=invocation.created_at,
        task_id=invocation.task_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        summary=f"{invocation.tool_name}:{invocation.status.value}",
        evidence=False,
        refs={
            "invocation_id": str(invocation.id),
            "operation_key": invocation.operation_key,
        },
        linked_observation_ids=tuple(observation.id for observation in observations),
    )


def _invocation_item(invocation: ToolInvocation) -> TimelineItemV1:
    actor_kind, actor_id = _invocation_actor(invocation)
    return TimelineItemV1(
        item_type=TimelineItemType.TOOL_INVOCATION,
        object_id=invocation.id,
        occurred_at=invocation.created_at,
        task_id=invocation.task_id,
        actor_kind=actor_kind,
        actor_id=actor_id,
        summary=f"{invocation.tool_name}:{invocation.status.value}",
        evidence=False,
        refs={
            "invocation_id": str(invocation.id),
            "operation_key": invocation.operation_key,
        },
    )


def _plan_item(
    plan: InvestigationPlanVersion, source_invocation: ToolInvocation | None
) -> TimelineItemV1:
    if source_invocation is not None:
        actor_kind, actor_id = _invocation_actor(source_invocation)
    else:
        actor_kind, actor_id = TimelineActorKind.SOURCE, "plan_investigation"
    return TimelineItemV1(
        item_type=TimelineItemType.PLAN_VERSION,
        object_id=plan.id,
        occurred_at=plan.created_at,
        task_id=None,
        actor_kind=actor_kind,
        actor_id=actor_id,
        summary=f"plan_version:{plan.version}:{plan.parse_status.value}",
        evidence=False,
        refs={
            "plan_version_id": str(plan.id),
            "source_invocation_id": str(plan.source_invocation_id),
            "source_observation_id": str(plan.source_observation_id),
        },
    )


def _handoff_item(record: HandoffRecord) -> TimelineItemV1:
    handoff = record.handoff
    refs = {
        "handoff_id": handoff.handoff_id,
        "task_id": handoff.task_id,
        "version": str(handoff.version),
        "sha256": handoff.content_sha256,
    }
    if record.predecessor_id is not None:
        refs["predecessor_id"] = record.predecessor_id
    return TimelineItemV1(
        item_type=TimelineItemType.HANDOFF,
        object_id=UUID(handoff.handoff_id),
        occurred_at=handoff.created_at,
        task_id=UUID(handoff.task_id),
        actor_kind=TimelineActorKind(record.created_by.kind.value),
        actor_id=record.created_by.actor_id,
        summary=(
            f"handoff:v{handoff.version}:"
            f"{handoff.from_actor.kind.value}->{handoff.to_actor.kind.value}"
        ),
        evidence=False,
        refs=refs,
    )


_CONTRIBUTION_ACTORS = {
    WorkContributionOrigin.HUMAN: TimelineActorKind.HUMAN,
    WorkContributionOrigin.AGENT: TimelineActorKind.OPERATOR,
    WorkContributionOrigin.HOST: TimelineActorKind.SYSTEM,
}


def _contribution_item(contribution: WorkContributionV1) -> TimelineItemV1:
    refs = {
        "contribution_id": str(contribution.id),
        "workspace_id": contribution.workspace_id,
        "workspace_revision_id": contribution.workspace_revision_id,
        "file_relpath": contribution.file_relpath,
        "file_sha256": contribution.file_sha256,
    }
    if contribution.observation_id is not None:
        refs["observation_id"] = str(contribution.observation_id)
    if contribution.finding_id is not None:
        refs["finding_id"] = str(contribution.finding_id)
    if contribution.validation_error_code is not None:
        refs["validation_error_code"] = contribution.validation_error_code.value
    return TimelineItemV1(
        item_type=TimelineItemType.CONTRIBUTION,
        object_id=contribution.id,
        occurred_at=contribution.observed_at,
        task_id=contribution.task_id,
        actor_kind=_CONTRIBUTION_ACTORS[contribution.origin],
        actor_id=contribution.origin_actor_id or contribution.origin.value,
        summary=f"contribution:{contribution.category.value}:{contribution.import_status.value}",
        evidence=False,
        refs=refs,
    )


def _agent_session_item(session: AgentSessionV1) -> TimelineItemV1:
    """A technical session; authentication material never enters the item."""
    refs = {
        "session_id": str(session.id),
        "task_id": str(session.task_id),
        "adapter_kind": session.adapter_kind.value,
        "state": session.state.value,
        "state_version": str(session.state_version),
    }
    if session.handoff_id is not None:
        refs["handoff_id"] = str(session.handoff_id)
    return TimelineItemV1(
        item_type=TimelineItemType.AGENT_SESSION,
        object_id=session.id,
        occurred_at=session.opened_at,
        task_id=session.task_id,
        actor_kind=TimelineActorKind.SYSTEM,
        actor_id=session.adapter_kind.value,
        summary=f"agent_session:{session.adapter_kind.value}:{session.state.value}",
        evidence=False,
        refs=refs,
    )


def _instruction_item(instruction: AgentSessionInstructionV1) -> TimelineItemV1:
    """One persisted human instruction with its text SHA-256.

    The terse timeline keeps identifiers, hashes and state only; the human
    text is read through the B6 session summary, and ``delivery_token`` never
    leaves the row.
    """
    refs = {
        "instruction_id": str(instruction.id),
        "session_id": str(instruction.session_id),
        "text_sha256": instruction.text_sha256,
        "delivery_attempts": str(instruction.delivery_attempts),
    }
    if instruction.delivery_mode is not None:
        refs["delivery_mode"] = instruction.delivery_mode.value
    if instruction.delivery_sha256 is not None:
        refs["delivery_sha256"] = instruction.delivery_sha256
    return TimelineItemV1(
        item_type=TimelineItemType.INSTRUCTION,
        object_id=instruction.id,
        occurred_at=instruction.queued_at,
        task_id=instruction.task_id,
        actor_kind=TimelineActorKind(instruction.created_by_kind),
        actor_id=instruction.created_by_id,
        summary=f"instruction:{instruction.state.value}",
        evidence=False,
        refs=refs,
    )


class HandoffTimelineSource:
    """Project persisted handoffs (lot 05A) as non-evidence items."""

    def __init__(self, handoffs: InvestigationHandoffRepository) -> None:
        self._handoffs = handoffs

    async def items(self, investigation_id: UUID) -> tuple[TimelineItemV1, ...]:
        return tuple(
            _handoff_item(record)
            for record in await self._handoffs.list_for_investigation(investigation_id)
        )


class ContributionTimelineSource:
    """Project append-only work contributions (lot 07) as non-evidence items."""

    def __init__(self, contributions: WorkContributionRepository) -> None:
        self._contributions = contributions

    async def items(self, investigation_id: UUID) -> tuple[TimelineItemV1, ...]:
        return tuple(
            _contribution_item(contribution)
            for contribution in await self._contributions.list_for_investigation(investigation_id)
        )


class AgentSessionTimelineSource:
    """Project technical agent sessions as non-evidence items."""

    def __init__(self, sessions: AgentSessionRepository) -> None:
        self._sessions = sessions

    async def items(self, investigation_id: UUID) -> tuple[TimelineItemV1, ...]:
        return tuple(
            _agent_session_item(session)
            for session in await self._sessions.list_for_investigation(investigation_id)
        )


class InstructionTimelineSource:
    """Project the persisted per-session human instruction queue."""

    def __init__(self, sessions: AgentSessionRepository) -> None:
        self._sessions = sessions

    async def items(self, investigation_id: UUID) -> tuple[TimelineItemV1, ...]:
        items: list[TimelineItemV1] = []
        for session in await self._sessions.list_for_investigation(investigation_id):
            items.extend(
                _instruction_item(instruction)
                for instruction in await self._sessions.list_instructions(session.id)
            )
        return tuple(items)


class InvestigationTimelineService:
    """Project the canonical records of one investigation into one page."""

    def __init__(
        self,
        *,
        tasks: InvestigationTaskRepository,
        decisions: AnalystDecisionRepository,
        observations: EvidenceObservationRepository,
        invocations: ToolInvocationRepository,
        plans: InvestigationPlanRepository,
        extra_sources: Sequence[TimelineSource] = (),
    ) -> None:
        self._tasks = tasks
        self._decisions = decisions
        self._observations = observations
        self._invocations = invocations
        self._plans = plans
        self._extra_sources = tuple(extra_sources)

    async def items(self, investigation_id: UUID) -> tuple[TimelineItemV1, ...]:
        tasks = await self._tasks.list_for_investigation(investigation_id)
        events: list[TaskEvent] = []
        for task in tasks:
            events.extend(await self._tasks.list_events(task.id))
        decisions = await self._decisions.list_for_investigation(investigation_id)
        observations = await self._observations.list_for_investigation(investigation_id)
        invocations = await self._invocations.list_for_investigation(investigation_id)
        plans = await self._plans.list_for_investigation(investigation_id)

        decision_by_id = {decision.id: decision for decision in decisions}
        events_by_decision: dict[UUID, list[TaskEvent]] = {
            decision.id: [] for decision in decisions
        }
        items: list[TimelineItemV1] = []
        for event in events:
            if event.analyst_decision_id in decision_by_id:
                events_by_decision[event.analyst_decision_id].append(event)
            else:
                items.append(_task_event_item(event))
        for decision in decisions:
            items.append(_decision_item(decision, events_by_decision[decision.id]))

        invocation_by_id = {invocation.id: invocation for invocation in invocations}
        absorbed_observations: set[UUID] = set()
        consultations: set[UUID] = set()
        for invocation in invocations:
            if invocation.tool_name != _ADVISOR_TOOL_NAME:
                continue
            related = sorted(
                (
                    observation
                    for observation in observations
                    if observation.invocation_ref == invocation.id
                    or observation.id in invocation.response_refs
                ),
                key=lambda observation: (observation.observed_at, observation.id),
            )
            advisory = [
                observation for observation in related if _is_advisory_observation(observation)
            ]
            absorbed_observations.update(observation.id for observation in advisory)
            consultations.add(invocation.id)
            items.append(_consultation_item(invocation, advisory))
        for observation in observations:
            if observation.id in absorbed_observations:
                continue
            items.append(_observation_item(observation))
        for invocation in invocations:
            if invocation.id in consultations:
                continue
            items.append(_invocation_item(invocation))
        for plan in plans:
            items.append(_plan_item(plan, invocation_by_id.get(plan.source_invocation_id)))

        for source in self._extra_sources:
            items.extend(await source.items(investigation_id))
        return tuple(items)

    async def page(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TimelinePageV1:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("timeline limit must be between 1 and 500")
        decoded = decode_position_cursor(cursor, investigation_id=investigation_id)
        if cursor is not None and decoded is None:
            raise ValueError("timeline cursor is invalid")
        ordered = sorted(
            await self.items(investigation_id),
            key=lambda item: (item.occurred_at, item.object_id),
        )
        if decoded is not None:
            ordered = [
                item
                for item in ordered
                if (item.occurred_at, item.object_id) > (decoded[0], decoded[1])
            ]
        page = ordered[:limit]
        has_more = len(ordered) > limit
        next_cursor = (
            encode_position_cursor(
                investigation_id=investigation_id,
                occurred_at=page[-1].occurred_at,
                item_id=page[-1].object_id,
            )
            if has_more and page
            else None
        )
        return TimelinePageV1(items=tuple(page), next_cursor=next_cursor, has_more=has_more)


__all__ = [
    "AgentSessionTimelineSource",
    "ContributionTimelineSource",
    "HandoffTimelineSource",
    "InstructionTimelineSource",
    "InvestigationTimelineService",
    "TimelineActorKind",
    "TimelineItemType",
    "TimelineItemV1",
    "TimelinePageV1",
    "TimelineSource",
    "decode_position_cursor",
    "encode_position_cursor",
]

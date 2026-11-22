"""Materialise an accepted plan version as durable investigation tasks.

ADR-0011 D2/D3/E6: a human acceptance writes ``plan_accept`` and then
materialises the parsed leads in the same unit of work.  Task identity is
deterministic (``lead-<sha256(investigation + NUL + lead_identity)[:24]>``), so
replaying an acceptance or accepting a later plan version never rewrites,
cancels or duplicates an existing task: only genuinely new leads are inserted.

Dependency references may name another lead of the same version (``lead_id``,
title) or an existing ``task_key`` of the investigation.  Before any insert the
service resolves every reference, refuses unresolved ones
(``plan_dependency_unresolved``) and runs a pure DAG check over the new and
existing task graph (``plan_dependency_cycle``).  A task is ``ready`` only when
all its dependencies are already ``completed``; otherwise it starts
``proposed``.  Insertion is serialised per investigation by the advisory lock
the repository already owns (M2).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from cti_app.application.investigation_persistence import (
    InvestigationTaskRepository,
    VersionConflictError,
)
from cti_app.application.investigation_plan_parser import (
    normalize_dependency_reference,
    plan_lead_identity_bytes,
)
from cti_app.application.investigation_task_events import (
    TaskEventDraft,
    TaskEventType,
    append_task_event,
)
from cti_app.domain.investigation_plan import InvestigationLeadV1, InvestigationPlanVersion
from cti_app.domain.investigation_task import (
    InvestigationTaskV1,
    ResultVerificationStatus,
    TaskActor,
    TaskActorKind,
    TaskPriority,
    TaskStatus,
)

TASK_KEY_PREFIX = "lead-"
_PLAN_ACCEPT_NAMESPACE = UUID("b6a1c2d3-4e5f-4a70-8b91-c2d3e4f50617")


class PlanMaterializationError(ValueError):
    """Base refusal for a plan that cannot produce tasks."""

    code = "plan_materialization_invalid"


class PlanDependencyUnresolvedError(PlanMaterializationError):
    """A ``depends_on`` reference names neither a lead nor an existing task."""

    code = "plan_dependency_unresolved"


class PlanDependencyCycleError(PlanMaterializationError):
    """The combined dependency graph is not a DAG."""

    code = "plan_dependency_cycle"


def plan_task_key(investigation_id: UUID, lead: InvestigationLeadV1) -> str:
    """The E10 deterministic task key for one lead."""
    return (
        TASK_KEY_PREFIX
        + hashlib.sha256(
            str(investigation_id).encode("utf-8") + b"\0" + plan_lead_identity_bytes(lead)
        ).hexdigest()[:24]
    )


def plan_accept_decision_id(investigation_id: UUID, idempotency_key: str) -> UUID:
    """A human acceptance is idempotent by ``(investigation, Idempotency-Key)``."""
    return uuid5(
        _PLAN_ACCEPT_NAMESPACE,
        f"plan_accept:{investigation_id}:{idempotency_key.strip()}",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanMaterializationPlanV1:
    """A validated, side-effect-free description of one acceptance."""

    version: InvestigationPlanVersion
    task_keys: tuple[str, ...]
    new_task_keys: tuple[str, ...]
    dependencies: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanMaterializationOutcomeV1:
    investigation_id: UUID
    plan_version_id: UUID
    created_task_ids: tuple[UUID, ...]
    tasks: tuple[InvestigationTaskV1, ...]


def _title_alias(title: str) -> str:
    return " ".join(title.casefold().split())


def _task_key_aliases(
    leads_by_key: Mapping[str, InvestigationLeadV1],
    task_keys: Sequence[str],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    ambiguous_titles: set[str] = set()
    for key in task_keys:
        lead = leads_by_key[key]
        aliases[key.casefold()] = key
        if lead.lead_id is not None:
            aliases.setdefault(lead.lead_id.casefold(), key)
        title = _title_alias(lead.title)
        if not title:
            continue
        if title in aliases and aliases[title] != key:
            ambiguous_titles.add(title)
        else:
            aliases[title] = key
    for title in ambiguous_titles:
        aliases.pop(title, None)
    return aliases


def _resolve_dependencies(
    leads_by_key: Mapping[str, InvestigationLeadV1],
    task_keys: Sequence[str],
    existing_keys: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    aliases = _task_key_aliases(leads_by_key, task_keys)
    for key in existing_keys:
        aliases.setdefault(key.casefold(), key)
    resolved: dict[str, tuple[str, ...]] = {}
    for key in task_keys:
        lead = leads_by_key[key]
        targets: list[str] = []
        for raw in lead.depends_on:
            token = normalize_dependency_reference(raw)
            target = aliases.get(token.casefold())
            if target is None:
                raise PlanDependencyUnresolvedError(
                    f"Dependency {raw!r} of lead {lead.title!r} is neither a lead "
                    "of this version nor an existing task key"
                )
            if target == key:
                raise PlanDependencyCycleError(f"Lead {lead.title!r} depends on itself")
            if target not in targets:
                targets.append(target)
        resolved[key] = tuple(targets)
    return resolved


def _ensure_acyclic(
    edges: Mapping[str, Sequence[str]],
    existing_edges: Mapping[str, Sequence[str]],
) -> None:
    graph: dict[str, set[str]] = {}
    for source, targets in (*existing_edges.items(), *edges.items()):
        graph.setdefault(source, set()).update(targets)
        for target in targets:
            graph.setdefault(target, set())
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise PlanDependencyCycleError("The plan dependency graph contains a cycle")
        visiting.add(node)
        for target in graph.get(node, ()):
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in tuple(graph):
        visit(node)


def _existing_edges(
    tasks: Sequence[InvestigationTaskV1],
    dependencies: Mapping[UUID, Sequence[UUID]],
) -> dict[str, tuple[str, ...]]:
    known = {task.id for task in tasks}
    return {
        str(task.id): tuple(str(dep) for dep in dependencies.get(task.id, ()) if dep in known)
        for task in tasks
    }


def _execution_policy(lead: InvestigationLeadV1) -> dict[str, object]:
    policy: dict[str, object] = {}
    if lead.tools:
        policy["tools"] = list(lead.tools)
    if lead.preconditions:
        policy["preconditions"] = list(lead.preconditions)
    if lead.interest:
        policy["interest"] = lead.interest
    if lead.conclusion_criteria:
        policy["conclusion_criteria"] = lead.conclusion_criteria
    if lead.limits:
        policy["limits"] = lead.limits
    return policy


class InvestigationPlanMaterializationService:
    def __init__(self, *, tasks: InvestigationTaskRepository) -> None:
        self._tasks = tasks

    async def prepare(self, version: InvestigationPlanVersion) -> PlanMaterializationPlanV1:
        """Resolve and validate every dependency before any write happens."""
        leads = version.content.leads
        task_keys = tuple(plan_task_key(version.investigation_id, lead) for lead in leads)
        leads_by_key = dict(zip(task_keys, leads, strict=True))
        existing = await self._tasks.list_for_investigation(version.investigation_id)
        existing_by_key = {task.task_key: task for task in existing}
        dependencies = _resolve_dependencies(
            leads_by_key,
            task_keys,
            tuple(existing_by_key),
        )
        dependencies_by_id = {
            task.id: await self._tasks.list_dependencies(task.id) for task in existing
        }
        node_by_key = {
            key: key if key not in existing_by_key else str(existing_by_key[key].id)
            for key in task_keys
        }
        existing_only_edges = _existing_edges(existing, dependencies_by_id)
        new_edges = {
            node_by_key[key]: tuple(node_by_key[target] for target in targets)
            for key, targets in dependencies.items()
            if key not in existing_by_key
        }
        _ensure_acyclic(new_edges, existing_only_edges)
        return PlanMaterializationPlanV1(
            version=version,
            task_keys=task_keys,
            new_task_keys=tuple(key for key in task_keys if key not in existing_by_key),
            dependencies=dependencies,
        )

    async def apply(
        self,
        plan: PlanMaterializationPlanV1,
        *,
        decision_id: UUID,
        now: datetime | None = None,
    ) -> PlanMaterializationOutcomeV1:
        """Insert new tasks, dependencies and ``task_created`` events atomically."""
        if not isinstance(decision_id, UUID):
            raise TypeError("decision_id must be a UUID")
        timestamp = now or datetime.now(UTC)
        version = plan.version
        investigation_id = version.investigation_id
        leads_by_key = dict(
            zip(
                plan.task_keys,
                version.content.leads,
                strict=True,
            )
        )
        actor = TaskActor(kind=TaskActorKind.SYSTEM, actor_id=f"plan_accept:{decision_id}")

        await self._tasks.lock_investigation(investigation_id)
        existing = await self._tasks.list_for_investigation(investigation_id)
        existing_by_key = {task.task_key: task for task in existing}
        dependencies_by_id = {
            task.id: await self._tasks.list_dependencies(task.id) for task in existing
        }
        node_by_key = {
            key: key if key not in existing_by_key else str(existing_by_key[key].id)
            for key in plan.task_keys
        }
        new_keys = tuple(key for key in plan.task_keys if key not in existing_by_key)
        new_edges = {
            node_by_key[key]: tuple(node_by_key[target] for target in plan.dependencies[key])
            for key in new_keys
        }
        _ensure_acyclic(new_edges, _existing_edges(existing, dependencies_by_id))

        created_ids: list[UUID] = []
        stored_by_key = dict(existing_by_key)
        for key in new_keys:
            lead = leads_by_key[key]
            target_keys = plan.dependencies[key]
            dependencies_completed = all(
                target in existing_by_key and existing_by_key[target].status is TaskStatus.COMPLETED
                for target in target_keys
            )
            task = InvestigationTaskV1(
                investigation_id=investigation_id,
                task_key=key,
                plan_version_id=version.id,
                title=lead.title,
                question=lead.objective or lead.title,
                objective=lead.objective,
                priority=TaskPriority.NORMAL,
                claim_refs=lead.claim_refs,
                evidence_refs=lead.start_evidence_refs,
                sample_refs=lead.sample_refs,
                execution_policy=_execution_policy(lead),
                status=(TaskStatus.READY if dependencies_completed else TaskStatus.PROPOSED),
                status_version=1,
                result_verification_status=ResultVerificationStatus.UNVERIFIED,
                created_by=actor,
                created_at=timestamp,
                updated_at=timestamp,
            )
            try:
                stored = await self._tasks.add(task)
            except VersionConflictError:
                # A concurrent materialisation of the same key won; never
                # rewrite the existing task from a later plan version.
                current = await self._tasks.get_by_task_key(investigation_id, key)
                if current is None:
                    raise
                assert current is not None
                stored = current
                continue
            stored_by_key[key] = stored
            created_ids.append(stored.id)

        for key in new_keys:
            new_task = stored_by_key.get(key)
            if new_task is None or new_task.plan_version_id != version.id:
                continue
            for target in plan.dependencies[key]:
                target_task = stored_by_key.get(target)
                if target_task is None:
                    raise PlanDependencyUnresolvedError(
                        f"Dependency {target!r} is no longer materialised"
                    )
                await self._tasks.add_dependency(
                    investigation_id=investigation_id,
                    task_id=new_task.id,
                    depends_on_task_id=target_task.id,
                )

        for key in new_keys:
            new_task = stored_by_key.get(key)
            if new_task is None or new_task.plan_version_id != version.id:
                continue
            draft = TaskEventDraft(
                investigation_id=investigation_id,
                task_id=new_task.id,
                event_type=TaskEventType.TASK_CREATED,
                actor=actor,
                idempotency_key=f"plan:{version.id}:{key}",
                to_status=new_task.status,
                analyst_decision_id=decision_id,
                payload={
                    "plan_version_id": str(version.id),
                    "plan_version": version.version,
                    "task_key": key,
                },
            )
            await append_task_event(self._tasks, draft, occurred_at=timestamp)

        materialised = tuple(stored_by_key[key] for key in plan.task_keys if key in stored_by_key)
        return PlanMaterializationOutcomeV1(
            investigation_id=investigation_id,
            plan_version_id=version.id,
            created_task_ids=tuple(created_ids),
            tasks=materialised,
        )

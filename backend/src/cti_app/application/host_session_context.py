"""Canonical per-session host context for an operator prepare (M24-X1).

``HostSessionContextProvider`` (lot B) is the narrow seam between the 05B1
session service and the host supervisor: a prepare needs the workspace
identity, the handoff reference, the task's path/skill envelope, the session
budgets and the model alias the human chose.  This module is the real
implementation and builds **only from canonical records**:

* ``workspace_id`` — the 0019 ``workspace_bindings`` row for the investigation;
  no host path is ever read or produced;
* ``handoff`` — the persisted ``investigation_handoffs`` content version and
  SHA-256, checked to belong to the same task and investigation;
* ``lease_fence`` — the task row's current monotone fence;
* ``allowed_workspace_paths`` — the task's declared relative paths;
* ``skill_names`` — the ``autowork-*`` entries of the task's required
  capabilities; the supervisor additionally resolves them under its own
  read-only skills root, so this list is an allowlist, never a discovery;
* ``budgets`` — the approved G2 envelope bounds, clamped by server settings;
* ``model`` — the alias the verified human chose at open, validated here.

External model use is a G2 decision: without an approved envelope that allows
it (and outside TLP RED) the provider refuses with a typed session error, so
an operator session can never start under a disclosure policy the human did
not approve.  Nothing here contacts the host: the returned value is data the
adapter maps onto the typed wire contract.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from cti_app.application.agentic_host_sessions import (
    HOST_MAX_ALLOWED_PATHS,
    HOST_MAX_SKILLS,
    HostBudgetV1,
    HostHandoffRefV1,
    HostSessionContextV1,
)
from cti_app.application.agentic_sessions import SessionError
from cti_app.application.investigation_persistence import (
    InvestigationEnvelopeRepository,
    InvestigationHandoffRepository,
    InvestigationTaskRepository,
    WorkspaceBindingRepository,
)
from cti_app.application.persistence import AnalystInvestigationRepository
from cti_app.config import Settings
from cti_app.domain.agentic_operator import OperatorAdapterKind
from cti_app.domain.investigation import BudgetDimension, InvestigationEnvelope, TLPLevel
from cti_app.domain.investigation_task import TaskActorKind, TaskStatus

# Bounds inherited from the host wire contract (§6) and the session defaults.
DEFAULT_SESSION_WALL_SECONDS = 3600
DEFAULT_MAX_INSTRUCTIONS = 64
MAX_HOST_OUTPUT_BYTES = 256 * 1024 * 1024
_SKILL_NAME_PATTERN = re.compile(r"^autowork-[a-z0-9][a-z0-9-]{0,63}$")
_MODEL_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_ACTIVE_SESSION_TASK_STATUSES = frozenset(
    {TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT}
)


class HostSessionContextError(SessionError):
    """A canonical context cannot be built; the session does not start."""

    code = "host_session_context_refused"
    status_code = 409


class HostSessionContextUnavailableError(HostSessionContextError):
    """The investigation has no bound workspace (host actions fail closed)."""

    code = "workspace_binding_missing"


class HostSessionContextForbiddenError(HostSessionContextError):
    """The G2 envelope does not authorize an external operator model."""

    code = "external_model_not_allowed"
    status_code = 403


class HostSessionContextUnitOfWork(Protocol):
    """The canonical stores one context resolution reads."""

    analyst_investigations: AnalystInvestigationRepository
    investigation_tasks: InvestigationTaskRepository
    investigation_handoffs: InvestigationHandoffRepository
    workspace_bindings: WorkspaceBindingRepository
    investigation_envelopes: InvestigationEnvelopeRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class CanonicalHostSessionContextProvider:
    """Build the host prepare context from canonical rows only (never a path)."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], HostSessionContextUnitOfWork],
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))

    async def host_session_context(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        adapter_kind: OperatorAdapterKind,
        handoff_id: UUID,
        requested_model: str | None = None,
    ) -> HostSessionContextV1:
        if not isinstance(adapter_kind, OperatorAdapterKind):
            raise TypeError("adapter_kind must use OperatorAdapterKind")
        model = _model_alias(requested_model)
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise HostSessionContextError(
                    f"investigation {investigation_id} does not exist",
                    details={"investigation_id": str(investigation_id)},
                )
            task = await uow.investigation_tasks.get(task_id)
            if task is None or task.investigation_id != investigation_id:
                raise HostSessionContextError(
                    f"task {task_id} is not part of investigation {investigation_id}",
                    details={"task_id": str(task_id)},
                )
            if task.assigned_actor_kind is not TaskActorKind.OPERATOR:
                raise HostSessionContextForbiddenError(
                    "an operator session requires a task assigned to an operator",
                    details={"task_id": str(task_id)},
                )
            if task.status not in _ACTIVE_SESSION_TASK_STATUSES:
                raise HostSessionContextError(
                    f"a {task.status.value} task cannot prepare an operator session",
                    details={"task_id": str(task_id), "status": task.status.value},
                )
            handoff = await uow.investigation_handoffs.get(handoff_id)
            if (
                handoff is None
                or handoff.handoff.task_id != str(task_id)
                or handoff.handoff.investigation_id != str(investigation_id)
            ):
                raise HostSessionContextError(
                    f"handoff {handoff_id} is not part of task {task_id}",
                    details={"handoff_id": str(handoff_id)},
                )
            binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
            if binding is None:
                raise HostSessionContextUnavailableError(
                    "the investigation has no bound host workspace",
                    details={"investigation_id": str(investigation_id)},
                )
            envelope = await uow.investigation_envelopes.get(investigation_id)
            _require_external_model(envelope)
            assert envelope is not None  # narrowed by _require_external_model
            try:
                return HostSessionContextV1(
                    workspace_id=binding.workspace_id,
                    handoff=HostHandoffRefV1(
                        handoff_id=handoff_id,
                        version=handoff.handoff.version,
                        sha256=handoff.handoff.content_sha256,
                    ),
                    lease_fence=task.lease_fence,
                    allowed_workspace_paths=_allowed_paths(task.allowed_workspace_paths),
                    skill_names=_skill_names(task.required_capabilities),
                    budgets=_session_budget(envelope, self._settings),
                    model=model,
                )
            except ValueError as exc:
                raise HostSessionContextError(
                    "the canonical session context is outside the host contract",
                    details={"session_id": str(session_id)},
                ) from exc


def _require_external_model(envelope: InvestigationEnvelope | None) -> None:
    """G2 fail-closed: no approved envelope, no external operator model."""
    if envelope is None:
        raise HostSessionContextForbiddenError(
            "no approved G2 envelope authorizes external model use for this investigation"
        )
    if not envelope.disclosure.external_llm_allowed:
        raise HostSessionContextForbiddenError(
            "the approved G2 envelope does not allow external model use"
        )
    if envelope.disclosure.tlp is TLPLevel.RED:
        raise HostSessionContextForbiddenError(
            "a TLP RED investigation never reaches an external operator model"
        )


def _model_alias(requested_model: str | None) -> str | None:
    if requested_model is None:
        return None
    if not isinstance(requested_model, str):
        raise HostSessionContextError("the requested model alias must be a string")
    normalized = requested_model.strip()
    if not normalized:
        return None
    if _MODEL_ALIAS_PATTERN.fullmatch(normalized) is None:
        raise HostSessionContextError(
            "the requested model alias is outside the host session pattern",
            details={"model": normalized[:128]},
        )
    return normalized


def _allowed_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    if len(paths) > HOST_MAX_ALLOWED_PATHS:
        raise HostSessionContextError(
            "the task declares more workspace paths than the host contract accepts",
            details={"count": str(len(paths))},
        )
    return tuple(paths)


def _skill_names(capabilities: tuple[str, ...]) -> tuple[str, ...]:
    """Only the task's ``autowork-*`` capabilities become a skill allowlist."""
    names = sorted({name for name in capabilities if _SKILL_NAME_PATTERN.fullmatch(name)})
    if len(names) > HOST_MAX_SKILLS:
        raise HostSessionContextError(
            "the task declares more skills than the host contract accepts",
            details={"count": str(len(names))},
        )
    return tuple(names)


def _session_budget(envelope: InvestigationEnvelope, settings: Settings) -> HostBudgetV1:
    """G2 bounds clamped by server settings; the host never receives more."""
    limits: Mapping[BudgetDimension, int] = envelope.budget.limits
    wall_seconds = _positive_limit(
        limits,
        BudgetDimension.TOOL_SECONDS,
        DEFAULT_SESSION_WALL_SECONDS,
        "the G2 envelope grants no operator session time",
    )
    context_bytes = limits.get(BudgetDimension.CONTEXT_BYTES)
    output_ceiling = max(1, settings.m24_max_contribution_bytes)
    max_output_bytes = (
        output_ceiling
        if context_bytes is None
        else max(1, min(context_bytes, output_ceiling, MAX_HOST_OUTPUT_BYTES))
    )
    return HostBudgetV1(
        wall_seconds=min(max(wall_seconds, 60), 24 * 60 * 60),
        max_output_bytes=max_output_bytes,
        max_instructions=DEFAULT_MAX_INSTRUCTIONS,
    )


def _positive_limit(
    limits: Mapping[BudgetDimension, int],
    dimension: BudgetDimension,
    default: int,
    refusal: str,
) -> int:
    value = limits.get(dimension)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HostSessionContextError(f"the G2 envelope bound {dimension.value} is invalid")
    if value == 0:
        raise HostSessionContextForbiddenError(refusal)
    return value


__all__ = [
    "CanonicalHostSessionContextProvider",
    "HostSessionContextError",
    "HostSessionContextForbiddenError",
    "HostSessionContextUnavailableError",
    "HostSessionContextUnitOfWork",
]

"""Create, read and freshness-check investigation handoffs (ADR-0011 D5).

The persisted ``investigation_handoffs`` row is the sole authority for a
handoff's content; this service builds the canonical
:class:`~cti_app.domain.investigation_handoff.InvestigationHandoffV1` payload,
verifies every reference against canonical stores, and appends it immutably.

Rules this module owns:

* **Idempotency** is scoped per task: ``(task_id, Idempotency-Key)``.  A retry
  with the same request body returns the stored version; the same key with a
  different body is a conflict.  ``request_sha256`` is the canonical identity of
  the caller's draft (task, parties, references, work, next action, key).
* **Versions** are monotone per task.  The task row is locked first, so two
  concurrent creates serialise and versions have no gaps or duplicates.
* **Operator authority** (D3): an operator-authored handoff is a write under
  the task lease.  The locked task row must still hold a live lease, and when
  the broker supplies the ``HandoffLeaseAuthority`` it verified, the lease
  session and fence must match it; a cancel, sweep or expiry committed before
  the append leaves no handoff row.
* **References** must belong to the same investigation and resolve.  Advisory
  observations (Bridge consultation material, D6) are never accepted as
  observation evidence refs or work-observation refs; a Bridge consultation is
  cited only by its persisted ``advisor.consult`` invocation.
* **Freshness** recomputes every ``freshness_refs`` entry against the canonical
  source and reports ``fresh`` or ``stale`` with visible diffs.  A changed human
  note revision or changed evidence never blocks the destination: it starts
  with a diff it can see.  :func:`assert_startable` is the separate pure gate
  lot 05B1 consumes: a changed task ``status_version`` or assignment raises
  ``handoff_task_stale``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cti_app.application.identity import is_human_actor_id
from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    EvidenceObservationRepository,
    FindingRepository,
    HandoffRecord,
    InvestigationHandoffRepository,
    InvestigationPlanRepository,
    InvestigationTaskRepository,
    ToolInvocationRepository,
    VersionConflictError,
    WorkspaceBindingRepository,
    WorkspaceFileRevisionRepository,
)
from cti_app.application.investigation_tasks import LEASE_HELD_STATUSES
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.application.workspace_agent import WorkspaceFileRevisionV1
from cti_app.domain.agentic_operator import AgentSessionInstructionV1
from cti_app.domain.editorial import AnalystDecision
from cti_app.domain.investigation import (
    EvidenceObservation,
    InvestigationFinding,
    ToolInvocation,
    canonical_json_bytes,
    logical_finding_id_by_version,
)
from cti_app.domain.investigation_handoff import (
    HANDOFF_MAX_TEXT_LENGTH,
    HandoffActor,
    HandoffActorKind,
    HandoffConsultationRef,
    HandoffDecisionRef,
    HandoffFileRef,
    HandoffFindingRef,
    HandoffFreshnessKind,
    HandoffFreshnessRef,
    HandoffFreshnessState,
    HandoffObservationRef,
    HandoffPlanRef,
    HandoffRef,
    HandoffRefKind,
    HandoffRefs,
    HandoffTaskSnapshot,
    HandoffWork,
    HandoffWorkItem,
    InvestigationHandoffV1,
)
from cti_app.domain.investigation_task import InvestigationTaskV1, TaskActorKind

ADVISOR_TOOL_NAME = "advisor.consult"
ADVISOR_RESPONSE_SOURCE = "advisor.consult.response"
ADVISOR_RESPONSE_OBJECT_TYPE = "advisor_response_received"


class HandoffError(ValueError):
    """Base refusal for a handoff operation, carrying an HTTP-stable code."""

    code = "handoff_invalid"
    status_code = 422

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


class HandoffNotFoundError(HandoffError):
    code = "handoff_not_found"
    status_code = 404


class HandoffRefUnresolvedError(HandoffError):
    code = "handoff_ref_unresolved"


class HandoffRefForbiddenError(HandoffError):
    code = "handoff_ref_forbidden"


class HandoffIdempotencyConflictError(HandoffError):
    code = "idempotency_conflict"
    status_code = 409


class HandoffVersionConflictError(HandoffError):
    code = "handoff_version_conflict"
    status_code = 409


class HandoffForbiddenError(HandoffError):
    code = "handoff_forbidden"
    status_code = 403


class HandoffTaskStaleError(HandoffError):
    """A start-session attempt against a handoff whose task moved (D5)."""

    code = "handoff_task_stale"
    status_code = 409


class HandoffLeaseAuthorityError(HandoffError):
    """An operator handoff was attempted without a current lease (D3/D5).

    Raised only after the task row was locked inside the transaction that
    appends the handoff: a lease revoked between the caller's pre-check and
    this point (cancel, sweep, reassignment, expiry) leaves no handoff row.
    """

    code = "handoff_lease_stale"
    status_code = 409


@dataclass(frozen=True, slots=True)
class HandoffLeaseAuthority:
    """The exact lease an agentic handoff is authored under (D3/D7).

    ``session_id``/``lease_fence`` are the values the broker verified before
    the call; the service re-reads the locked task row and refuses a mismatch,
    so a superseded session or fence can never append a handoff.
    """

    task_id: UUID
    session_id: UUID
    lease_fence: int

    def __post_init__(self) -> None:
        for name in ("task_id", "session_id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be a UUID")
        if isinstance(self.lease_fence, bool) or not isinstance(self.lease_fence, int):
            raise TypeError("lease_fence must be an integer")
        if self.lease_fence < 1:
            raise ValueError("lease_fence must be >= 1")


class HandoffUnitOfWork(Protocol):
    """The canonical stores a handoff operation reads or appends."""

    investigation_handoffs: InvestigationHandoffRepository
    investigation_tasks: InvestigationTaskRepository
    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    evidence_observations: EvidenceObservationRepository
    investigation_findings: FindingRepository
    tool_invocations: ToolInvocationRepository
    investigation_plans: InvestigationPlanRepository
    workspace_bindings: WorkspaceBindingRepository
    workspace_file_revisions: WorkspaceFileRevisionRepository
    agent_sessions: AgentSessionRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class HandoffCreateRequest(BaseModel):
    """The caller's draft; identity, version, plan and hashes are derived.

    The client cites canonical objects by id.  The service resolves each one,
    reads its authoritative hash/version and refuses anything that does not
    belong to the same investigation, so a handoff can never record a value the
    database does not hold.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    to_actor: HandoffActor
    work: HandoffWork = HandoffWork()
    observation_ids: tuple[UUID, ...] = ()
    finding_version_ids: tuple[UUID, ...] = ()
    decision_ids: tuple[UUID, ...] = ()
    consultation_invocation_ids: tuple[UUID, ...] = ()
    file_revision_ids: tuple[str, ...] = ()
    next_action: str = Field(min_length=1, max_length=HANDOFF_MAX_TEXT_LENGTH)

    @field_validator("next_action")
    @classmethod
    def _strip_next_action(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("next_action must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_request(self) -> HandoffCreateRequest:
        if self.to_actor.kind is HandoffActorKind.SYSTEM:
            raise ValueError("a handoff recipient is human or operator, never system")
        for name in (
            "observation_ids",
            "finding_version_ids",
            "decision_ids",
            "consultation_invocation_ids",
            "file_revision_ids",
        ):
            values = tuple(getattr(self, name))
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not repeat a reference")
        return self

    def request_sha256(self, from_actor: HandoffActor, idempotency_key: str) -> str:
        payload = {
            "request": self.model_dump(mode="json"),
            "from_actor": from_actor.model_dump(mode="json"),
            "idempotency_key": idempotency_key,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class HandoffFreshnessDiff(BaseModel):
    """One recorded reference value that no longer matches the canonical store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: HandoffFreshnessKind
    id: str
    recorded: str
    current: str | None


_DiffList = list[HandoffFreshnessDiff]


class HandoffFreshnessReport(BaseModel):
    """Outcome of recomputing every freshness ref of one handoff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handoff_id: str
    state: HandoffFreshnessState
    diffs: tuple[HandoffFreshnessDiff, ...] = ()


@dataclass(frozen=True, slots=True)
class _ResolvedRefs:
    refs: HandoffRefs
    decision_digests: dict[str, str]


class InvestigationHandoffService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], HandoffUnitOfWork],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(
        self,
        investigation_id: UUID,
        *,
        actor: HandoffActor,
        idempotency_key: str,
        request: HandoffCreateRequest,
        now: datetime | None = None,
        authority: HandoffLeaseAuthority | None = None,
    ) -> HandoffRecord:
        """Append one immutable handoff version inside the caller's UoW.

        An operator handoff is a write under the task lease (D3): the locked
        task row must still hold a live lease, and when the broker supplies the
        exact ``authority`` it verified, the session and fence must match it.
        The check runs after the replay lookup and before the append, so a
        retry returns the recorded version while a revoked lease appends
        nothing.
        """
        if not isinstance(investigation_id, UUID):
            raise TypeError("investigation_id must be a UUID")
        if not isinstance(actor, HandoffActor):
            raise TypeError("actor must use HandoffActor")
        if not isinstance(request, HandoffCreateRequest):
            raise TypeError("request must use HandoffCreateRequest")
        if authority is not None and not isinstance(authority, HandoffLeaseAuthority):
            raise TypeError("authority must use HandoffLeaseAuthority")
        if not idempotency_key.strip():
            raise ValueError("Idempotency-Key must not be empty")
        self._require_actor_may_handoff(actor)
        request_sha256 = request.request_sha256(actor, idempotency_key)
        timestamp = now if now is not None else self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise HandoffNotFoundError(f"investigation {investigation_id} does not exist")
            task = await uow.investigation_tasks.lock_task(request.task_id)
            if task is None or task.investigation_id != investigation_id:
                raise HandoffNotFoundError(
                    f"task {request.task_id} is not part of investigation {investigation_id}"
                )
            self._require_actor_owns_task(actor, task)

            existing = await uow.investigation_handoffs.get_by_idempotency(task.id, idempotency_key)
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise HandoffIdempotencyConflictError(
                        "Idempotency-Key already records a different handoff request"
                    )
                return existing

            await self._require_lease_authority(uow, task, actor, authority)
            latest = await uow.investigation_handoffs.latest_for_task(task.id)
            version = 1 if latest is None else latest.handoff.version + 1
            predecessor_id = latest.handoff.handoff_id if latest is not None else None
            plan = await self._resolve_plan(uow, task)
            resolver = _RefResolver(uow, investigation_id)
            resolved = await self._resolve_refs(resolver, request)
            files = await self._resolve_files(resolver, request.file_revision_ids)
            await self._verify_work_refs(resolver, request.work, task.id)
            freshness = self._freshness_refs(resolved, files)
            try:
                handoff = InvestigationHandoffV1(
                    handoff_id=str(uuid4()),
                    investigation_id=str(investigation_id),
                    task_id=str(task.id),
                    version=version,
                    created_at=timestamp,
                    from_actor=actor,
                    to_actor=request.to_actor,
                    task=HandoffTaskSnapshot(
                        status=task.status,
                        status_version=task.status_version,
                        question=task.question,
                        objective=task.objective,
                        expected_outputs=task.expected_outputs,
                        assigned_actor_kind=(
                            HandoffActorKind(task.assigned_actor_kind.value)
                            if task.assigned_actor_kind is not None
                            else None
                        ),
                        assigned_actor_id=task.assigned_actor_id,
                    ),
                    plan=plan,
                    work=request.work,
                    refs=resolved.refs,
                    files=files,
                    next_action=request.next_action,
                    freshness_refs=freshness,
                )
            except ValueError as exc:
                raise HandoffError(str(exc)) from exc
            record = HandoffRecord(
                handoff=handoff,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                created_by=actor,
                predecessor_id=predecessor_id,
            )
            try:
                stored, created = await uow.investigation_handoffs.append_if_absent(record)
            except VersionConflictError as exc:
                raise HandoffVersionConflictError(str(exc)) from exc
            # The task row points at its current handoff (0021/0022 FK): the
            # cockpit resolves the destination handoff from this projection
            # before it may open the operator session.  The handoff row is
            # already INSERTed above, so the FK is satisfied.
            if created:
                # A replayed (older) handoff must never regress the pointer.
                await uow.investigation_tasks.set_handoff_ref(
                    task.id, UUID(stored.handoff.handoff_id)
                )
            await uow.commit()
            return stored

    async def get(self, investigation_id: UUID, handoff_id: UUID) -> HandoffRecord:
        async with self._uow_factory() as uow:
            record = await uow.investigation_handoffs.get(handoff_id)
            if record is None or record.handoff.investigation_id != str(investigation_id):
                raise HandoffNotFoundError(f"handoff {handoff_id} does not exist")
            return record

    async def list(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[HandoffRecord, ...]:
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise HandoffNotFoundError(f"investigation {investigation_id} does not exist")
            return await uow.investigation_handoffs.list_for_investigation(
                investigation_id, task_id=task_id
            )

    async def freshness(self, investigation_id: UUID, handoff_id: UUID) -> HandoffFreshnessReport:
        """Recompute every freshness ref against its canonical source."""
        async with self._uow_factory() as uow:
            record = await uow.investigation_handoffs.get(handoff_id)
            handoff = record.handoff if record is not None else None
            if handoff is None or handoff.investigation_id != str(investigation_id):
                raise HandoffNotFoundError(f"handoff {handoff_id} does not exist")
            diffs = await self._freshness_diffs(uow, handoff)
        return HandoffFreshnessReport(
            handoff_id=handoff.handoff_id,
            state=HandoffFreshnessState.STALE if diffs else HandoffFreshnessState.FRESH,
            diffs=tuple(diffs),
        )

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _require_actor_may_handoff(actor: HandoffActor) -> None:
        if actor.kind is HandoffActorKind.SYSTEM:
            raise HandoffForbiddenError("the system actor never authors a handoff")
        if actor.kind is HandoffActorKind.HUMAN and not is_human_actor_id(actor.actor_id):
            raise HandoffForbiddenError("a human handoff requires a human actor id")

    @staticmethod
    def _require_actor_owns_task(actor: HandoffActor, task: InvestigationTaskV1) -> None:
        if actor.kind is not HandoffActorKind.OPERATOR:
            return
        if (
            task.assigned_actor_kind is not TaskActorKind.OPERATOR
            or task.assigned_actor_id != actor.actor_id
        ):
            raise HandoffForbiddenError(
                "an operator may only hand off a task assigned to that operator",
                details={"task_id": str(task.id)},
            )

    @staticmethod
    async def _require_lease_authority(
        uow: HandoffUnitOfWork,
        task: InvestigationTaskV1,
        actor: HandoffActor,
        authority: HandoffLeaseAuthority | None,
    ) -> None:
        """Revalidate an operator handoff under the locked task row (D3/D7).

        Runs after the replay lookup (a retry still returns its recorded
        version) and before the append, so a cancel, sweep, expiry or
        reassignment committed before this transaction refuses the write.
        """
        if actor.kind is not HandoffActorKind.OPERATOR:
            if authority is not None:
                raise HandoffLeaseAuthorityError(
                    "only an operator handoff carries a lease authority"
                )
            return
        if (
            task.status not in LEASE_HELD_STATUSES
            or task.lease_session_id is None
            or task.lease_expires_at is None
        ):
            raise HandoffLeaseAuthorityError(
                "the operator task holds no live lease",
                details={"task_id": str(task.id)},
            )
        if authority is not None:
            if authority.task_id != task.id:
                raise HandoffLeaseAuthorityError(
                    "the handoff authority targets another task",
                    details={"task_id": str(task.id)},
                )
            if (
                task.lease_session_id != authority.session_id
                or task.lease_fence != authority.lease_fence
            ):
                raise HandoffLeaseAuthorityError(
                    "the agent lease session or fence is stale",
                    details={"code": "lease_fence_stale", "task_id": str(task.id)},
                )
        if task.lease_expires_at <= await uow.investigation_tasks.database_now():
            raise HandoffLeaseAuthorityError(
                "the agent lease has expired",
                details={"code": "lease_expired", "task_id": str(task.id)},
            )

    @staticmethod
    async def _resolve_plan(
        uow: HandoffUnitOfWork, task: InvestigationTaskV1
    ) -> HandoffPlanRef | None:
        if task.plan_version_id is None:
            return None
        version = await uow.investigation_plans.get_version(task.plan_version_id)
        if version is None or version.investigation_id != task.investigation_id:
            raise HandoffRefUnresolvedError(
                f"task plan version {task.plan_version_id} does not resolve"
            )
        return HandoffPlanRef(
            plan_version_id=str(version.id),
            plan_version=version.version,
            plan_sha256=version.content_sha256,
        )

    @staticmethod
    async def _resolve_refs(
        resolver: _RefResolver,
        request: HandoffCreateRequest,
    ) -> _ResolvedRefs:
        observations: list[HandoffObservationRef] = []
        for observation_id in request.observation_ids:
            observation = await resolver.observation(observation_id)
            observations.append(
                HandoffObservationRef(id=str(observation.id), sha256=observation.sha256)
            )
        findings: list[HandoffFindingRef] = []
        for version_id in request.finding_version_ids:
            finding = await resolver.finding(version_id)
            findings.append(HandoffFindingRef(version_id=str(finding.id), version=finding.version))
        decisions: list[HandoffDecisionRef] = []
        decision_digests: dict[str, str] = {}
        for decision_id in request.decision_ids:
            decision = await resolver.decision(decision_id)
            decisions.append(HandoffDecisionRef(analyst_decision_id=str(decision.id)))
            decision_digests[str(decision.id)] = _decision_digest(decision)
        consultations: list[HandoffConsultationRef] = []
        for invocation_id in request.consultation_invocation_ids:
            invocation, consultation_type, output_sha256 = await resolver.consultation(
                invocation_id
            )
            consultations.append(
                HandoffConsultationRef(
                    invocation_id=str(invocation.id),
                    consultation_type=consultation_type,
                    output_sha256=output_sha256,
                )
            )
        return _ResolvedRefs(
            refs=HandoffRefs(
                observations=tuple(observations),
                findings=tuple(findings),
                decisions=tuple(decisions),
                consultations=tuple(consultations),
            ),
            decision_digests=decision_digests,
        )

    @staticmethod
    async def _resolve_files(
        resolver: _RefResolver,
        revision_ids: Sequence[str],
    ) -> tuple[HandoffFileRef, ...]:
        files: list[HandoffFileRef] = []
        for revision_id in revision_ids:
            revision = await resolver.file(revision_id)
            files.append(
                HandoffFileRef(
                    workspace_revision_id=revision.revision_id,
                    file_relpath=revision.path,
                    file_sha256=revision.sha256,
                    observed_at=revision.observed_at,
                )
            )
        return tuple(files)

    @staticmethod
    async def _verify_work_refs(resolver: _RefResolver, work: HandoffWork, task_id: UUID) -> None:
        for item in _work_items(work):
            for ref in item.refs:
                await _resolve_work_ref(resolver, ref, task_id)

    @staticmethod
    def _freshness_refs(
        resolved: _ResolvedRefs, files: Sequence[HandoffFileRef]
    ) -> tuple[HandoffFreshnessRef, ...]:
        refs: list[HandoffFreshnessRef] = []
        for observation in resolved.refs.observations:
            refs.append(
                HandoffFreshnessRef(
                    kind=HandoffFreshnessKind.OBSERVATION,
                    id=observation.id,
                    sha256=observation.sha256,
                )
            )
        for finding in resolved.refs.findings:
            refs.append(
                HandoffFreshnessRef(
                    kind=HandoffFreshnessKind.FINDING,
                    id=finding.version_id,
                    version=finding.version,
                )
            )
        for decision in resolved.refs.decisions:
            refs.append(
                HandoffFreshnessRef(
                    kind=HandoffFreshnessKind.DECISION,
                    id=decision.analyst_decision_id,
                    sha256=resolved.decision_digests[decision.analyst_decision_id],
                )
            )
        for consultation in resolved.refs.consultations:
            refs.append(
                HandoffFreshnessRef(
                    kind=HandoffFreshnessKind.CONSULTATION,
                    id=consultation.invocation_id,
                    sha256=consultation.output_sha256,
                )
            )
        for file in files:
            refs.append(
                HandoffFreshnessRef(
                    kind=HandoffFreshnessKind.FILE,
                    id=file.workspace_revision_id,
                    sha256=file.file_sha256,
                )
            )
        return tuple(refs)

    async def _freshness_diffs(
        self, uow: HandoffUnitOfWork, handoff: InvestigationHandoffV1
    ) -> _DiffList:
        diffs: _DiffList = []
        files_by_revision = {item.workspace_revision_id: item for item in handoff.files}
        observations: dict[UUID, EvidenceObservation] | None = None
        findings: tuple[InvestigationFinding, ...] | None = None
        decisions: dict[UUID, AnalystDecision] | None = None
        invocations: tuple[ToolInvocation, ...] | None = None
        revisions: dict[str, WorkspaceFileRevisionV1] | None = None
        workspace_id: str | None = None
        workspace_loaded = False

        for ref in handoff.freshness_refs:
            recorded = str(ref.version) if ref.version is not None else str(ref.sha256)
            if ref.kind is HandoffFreshnessKind.OBSERVATION:
                if observations is None:
                    observations = {
                        item.id: item
                        for item in await uow.evidence_observations.list_for_investigation(
                            UUID(handoff.investigation_id)
                        )
                    }
                observation = observations.get(UUID(ref.id))
                current = observation.sha256 if observation is not None else None
            elif ref.kind is HandoffFreshnessKind.FINDING:
                if findings is None:
                    findings = await uow.investigation_findings.list_for_investigation(
                        UUID(handoff.investigation_id)
                    )
                current = _current_finding_version(findings, UUID(ref.id))
            elif ref.kind is HandoffFreshnessKind.DECISION:
                if decisions is None:
                    decisions = {
                        item.id: item
                        for item in await uow.analyst_decisions.list_for_investigation(
                            UUID(handoff.investigation_id)
                        )
                    }
                decision = decisions.get(UUID(ref.id))
                current = _decision_digest(decision) if decision is not None else None
            elif ref.kind is HandoffFreshnessKind.CONSULTATION:
                if invocations is None:
                    invocations = await uow.tool_invocations.list_for_investigation(
                        UUID(handoff.investigation_id)
                    )
                current = await _consultation_current_sha(uow, invocations, UUID(ref.id))
            else:
                file = files_by_revision.get(ref.id)
                if file is None:
                    current = None
                else:
                    if not workspace_loaded:
                        binding = await uow.workspace_bindings.get_for_investigation(
                            UUID(handoff.investigation_id)
                        )
                        workspace_id = binding.workspace_id if binding is not None else None
                        revisions = (
                            {
                                item.revision_id: item
                                for item in await uow.workspace_file_revisions.list_for_workspace(
                                    workspace_id
                                )
                            }
                            if workspace_id is not None
                            else {}
                        )
                        workspace_loaded = True
                    current = _current_file_sha(
                        tuple(revisions.values()) if revisions is not None else (),
                        file.file_relpath,
                    )
            if current != recorded:
                diffs.append(
                    HandoffFreshnessDiff(
                        kind=ref.kind,
                        id=ref.id,
                        recorded=recorded,
                        current=current,
                    )
                )
        return diffs


def assert_startable(handoff: InvestigationHandoffV1, task: InvestigationTaskV1) -> None:
    """Refuse a stale handoff before a session starts (D5, consumed by 05B1).

    Pure: it compares the handoff's task snapshot with the current task row.
    A status-version or assignment change raises ``handoff_task_stale``.
    """
    if not isinstance(handoff, InvestigationHandoffV1):
        raise TypeError("handoff must use InvestigationHandoffV1")
    if not isinstance(task, InvestigationTaskV1):
        raise TypeError("task must use InvestigationTaskV1")
    if str(task.id) != handoff.task_id or str(task.investigation_id) != handoff.investigation_id:
        raise HandoffTaskStaleError("the handoff belongs to another task or investigation")
    if task.status_version != handoff.task.status_version:
        raise HandoffTaskStaleError(
            "the task status version changed since the handoff was created",
            details={
                "recorded_status_version": handoff.task.status_version,
                "current_status_version": task.status_version,
            },
        )
    current_kind = task.assigned_actor_kind.value if task.assigned_actor_kind is not None else None
    recorded_kind = (
        handoff.task.assigned_actor_kind.value
        if handoff.task.assigned_actor_kind is not None
        else None
    )
    if (current_kind, task.assigned_actor_id) != (recorded_kind, handoff.task.assigned_actor_id):
        raise HandoffTaskStaleError(
            "the task assignment changed since the handoff was created",
            details={"recorded_assignment": recorded_kind, "current_assignment": current_kind},
        )


def _work_items(work: HandoffWork) -> tuple[HandoffWorkItem, ...]:
    return (
        *work.hypotheses,
        *work.attempts,
        *work.established_results,
        *work.partial_results,
        *work.negative_results,
        *work.limits,
    )


async def _resolve_work_ref(resolver: _RefResolver, ref: HandoffRef, task_id: UUID) -> None:
    if ref.kind is HandoffRefKind.FILE:
        await resolver.file(ref.id)
        return
    try:
        identifier = UUID(ref.id)
    except ValueError as exc:
        raise HandoffRefUnresolvedError(
            f"work reference {ref.kind.value}:{ref.id} is not a canonical id"
        ) from exc
    if ref.kind is HandoffRefKind.OBSERVATION:
        await resolver.observation(identifier)
    elif ref.kind is HandoffRefKind.FINDING:
        await resolver.finding(identifier)
    elif ref.kind is HandoffRefKind.DECISION:
        await resolver.decision(identifier)
    elif ref.kind is HandoffRefKind.CONSULTATION:
        await resolver.consultation(identifier)
    elif ref.kind is HandoffRefKind.INSTRUCTION:
        await resolver.instruction(identifier, task_id)
    else:  # pragma: no cover - the enum is closed
        raise HandoffRefUnresolvedError(f"unsupported work reference {ref.kind.value}")


def _decision_digest(decision: AnalystDecision) -> str:
    payload = {
        "analyst_decision_id": str(decision.id),
        "investigation_id": str(decision.investigation_id),
        "decision_type": decision.decision_type.value,
        "target_type": decision.target_type.value,
        "target_id": str(decision.target_id),
        "actor_id": decision.actor_id,
        "reason": decision.reason,
        "correlation_id": decision.correlation_id,
        "details": decision.details,
        "occurred_at": decision.occurred_at.isoformat(),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _current_finding_version(
    findings: Sequence[InvestigationFinding], version_id: UUID
) -> str | None:
    by_id = {finding.id: finding for finding in findings}
    referenced = by_id.get(version_id)
    if referenced is None:
        return None
    latest_id = logical_finding_id_by_version(findings).get(version_id)
    latest = by_id.get(latest_id) if latest_id is not None else None
    if latest is None:
        return None
    return str(latest.version)


def _current_file_sha(revisions: Sequence[WorkspaceFileRevisionV1], relpath: str) -> str | None:
    matching = [revision for revision in revisions if revision.path == relpath]
    if not matching:
        return None
    latest = max(matching, key=lambda item: (item.observed_at, item.revision_id))
    return str(latest.sha256)


async def _consultation_current_sha(
    uow: HandoffUnitOfWork, invocations: Sequence[ToolInvocation], invocation_id: UUID
) -> str | None:
    invocation = next((item for item in invocations if item.id == invocation_id), None)
    if invocation is None:
        return None
    return await _consultation_output_sha256(uow, invocation)


async def _consultation_output_sha256(
    uow: HandoffUnitOfWork, invocation: ToolInvocation
) -> str | None:
    output_shas: set[str] = set()
    for reference in invocation.response_refs:
        observation = await uow.evidence_observations.get(reference)
        if observation is None:
            continue
        if (
            observation.source == ADVISOR_RESPONSE_SOURCE
            or observation.object_type == ADVISOR_RESPONSE_OBJECT_TYPE
        ):
            output_shas.add(observation.sha256)
    if len(output_shas) != 1:
        return None
    return output_shas.pop()


def _is_advisory_observation(observation: EvidenceObservation) -> bool:
    """D6 consultative-observation rule; lot 07 centralises the helper."""
    return (
        (observation.source == "advisor.consult" and observation.object_type == "advisor_call")
        or (
            observation.source == ADVISOR_RESPONSE_SOURCE
            and observation.object_type == ADVISOR_RESPONSE_OBJECT_TYPE
        )
        or observation.source.startswith("advisor.")
        or observation.object_type.startswith("advisor_")
    )


class _RefResolver:
    """Resolve and verify references against canonical stores, with caching."""

    def __init__(self, uow: HandoffUnitOfWork, investigation_id: UUID) -> None:
        self._uow = uow
        self._investigation_id = investigation_id
        self._observations: dict[UUID, EvidenceObservation] = {}
        self._findings: dict[UUID, InvestigationFinding] = {}
        self._decisions: dict[UUID, AnalystDecision] | None = None
        self._invocations: dict[UUID, ToolInvocation] = {}
        self._instructions: dict[UUID, AgentSessionInstructionV1] = {}
        self._workspace_id: str | None = None
        self._workspace_loaded = False
        self._revisions: dict[str, WorkspaceFileRevisionV1] = {}

    async def observation(self, observation_id: UUID) -> EvidenceObservation:
        observation = self._observations.get(observation_id)
        if observation is None:
            observation = await self._uow.evidence_observations.get(observation_id)
            if observation is None:
                raise HandoffRefUnresolvedError(f"observation {observation_id} does not exist")
            self._observations[observation_id] = observation
        if observation.investigation_id != self._investigation_id:
            raise HandoffRefUnresolvedError(
                f"observation {observation_id} belongs to another investigation"
            )
        if _is_advisory_observation(observation):
            raise HandoffRefForbiddenError(
                f"advisory observation {observation_id} is not evidence; cite its "
                "advisor.consult invocation as a consultation ref instead",
                details={"observation_id": str(observation_id)},
            )
        return observation

    async def finding(self, version_id: UUID) -> InvestigationFinding:
        finding = self._findings.get(version_id)
        if finding is None:
            finding = await self._uow.investigation_findings.get(version_id)
            if finding is None:
                raise HandoffRefUnresolvedError(f"finding version {version_id} does not exist")
            self._findings[version_id] = finding
        if finding.investigation_id != self._investigation_id:
            raise HandoffRefUnresolvedError(
                f"finding version {version_id} belongs to another investigation"
            )
        return finding

    async def decision(self, decision_id: UUID) -> AnalystDecision:
        if self._decisions is None:
            self._decisions = {
                item.id: item
                for item in await self._uow.analyst_decisions.list_for_investigation(
                    self._investigation_id
                )
            }
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise HandoffRefUnresolvedError(
                f"analyst decision {decision_id} does not exist in this investigation"
            )
        return decision

    async def consultation(self, invocation_id: UUID) -> tuple[ToolInvocation, str, str]:
        invocation = self._invocations.get(invocation_id)
        if invocation is None:
            invocation = await self._uow.tool_invocations.get(invocation_id)
            if invocation is None:
                raise HandoffRefUnresolvedError(f"tool invocation {invocation_id} does not exist")
            self._invocations[invocation_id] = invocation
        if invocation.investigation_id != self._investigation_id:
            raise HandoffRefUnresolvedError(
                f"tool invocation {invocation_id} belongs to another investigation"
            )
        if invocation.tool_name != ADVISOR_TOOL_NAME:
            raise HandoffRefForbiddenError(
                f"invocation {invocation_id} is not an advisor.consult consultation"
            )
        consultation_type = invocation.normalized_parameters.get("consultation_type")
        if not isinstance(consultation_type, str) or not consultation_type.strip():
            raise HandoffRefUnresolvedError(
                f"consultation {invocation_id} has no persisted consultation type"
            )
        output_sha256 = await _consultation_output_sha256(self._uow, invocation)
        if output_sha256 is None:
            raise HandoffRefUnresolvedError(
                f"consultation {invocation_id} has no single persisted advisor response"
            )
        return invocation, consultation_type, output_sha256

    async def instruction(self, instruction_id: UUID, task_id: UUID) -> AgentSessionInstructionV1:
        """A cited session instruction must belong to this task and investigation."""
        instruction = self._instructions.get(instruction_id)
        if instruction is None:
            instruction = await self._uow.agent_sessions.get_instruction(instruction_id)
            if instruction is None:
                raise HandoffRefUnresolvedError(f"instruction {instruction_id} does not exist")
            self._instructions[instruction_id] = instruction
        if instruction.investigation_id != self._investigation_id:
            raise HandoffRefUnresolvedError(
                f"instruction {instruction_id} belongs to another investigation"
            )
        if instruction.task_id != task_id:
            raise HandoffRefUnresolvedError(f"instruction {instruction_id} belongs to another task")
        return instruction

    async def file(self, revision_id: str) -> WorkspaceFileRevisionV1:
        revision = self._revisions.get(revision_id)
        if revision is None:
            workspace_id = await self._workspace()
            if workspace_id is None:
                raise HandoffRefUnresolvedError("the investigation has no bound workspace")
            revision = await self._uow.workspace_file_revisions.get(workspace_id, revision_id)
            if revision is None:
                raise HandoffRefUnresolvedError(f"workspace revision {revision_id} does not exist")
            self._revisions[revision_id] = revision
        return revision

    async def _workspace(self) -> str | None:
        if not self._workspace_loaded:
            binding = await self._uow.workspace_bindings.get_for_investigation(
                self._investigation_id
            )
            self._workspace_id = binding.workspace_id if binding is not None else None
            self._workspace_loaded = True
        return self._workspace_id


__all__ = [
    "HandoffCreateRequest",
    "HandoffError",
    "HandoffForbiddenError",
    "HandoffFreshnessDiff",
    "HandoffFreshnessReport",
    "HandoffIdempotencyConflictError",
    "HandoffLeaseAuthority",
    "HandoffLeaseAuthorityError",
    "HandoffNotFoundError",
    "HandoffRefForbiddenError",
    "HandoffRefUnresolvedError",
    "HandoffTaskStaleError",
    "HandoffUnitOfWork",
    "HandoffVersionConflictError",
    "InvestigationHandoffService",
    "assert_startable",
]

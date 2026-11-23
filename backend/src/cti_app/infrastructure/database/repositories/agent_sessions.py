"""SQLAlchemy repository for agent sessions and their instruction queue (M24-05B1).

The partial unique index ``(task_id) WHERE state IN (<active>)`` and the
``(task_id, idempotency_key)`` / ``(session_id, idempotency_key)`` uniques are
the database's last-resort race guards; the service decides replay semantics
first.  ``update_state`` is the ``state_version`` CAS and never touches a task
row: the lease authority stays in ``investigation_tasks`` (lot 04B2).

``is_active_session``/``close_session`` implement the narrow ``TaskSessionPort``
seam the task engine consumes; ``close_session`` is idempotent on an already
closed or lost session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Float, String, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.domain.agentic_operator import (
    ACTIVE_AGENT_SESSION_STATES,
    INSTRUCTION_PENDING_STATES,
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionV1,
    InstructionDeliveryMode,
    InstructionState,
    OperatorAdapterKind,
    SessionTransitionError,
)
from cti_app.domain.investigation import thaw_json
from cti_app.infrastructure.database.models.agent_sessions import (
    AgentSessionInstructionRow,
    AgentSessionRow,
)
from cti_app.infrastructure.database.models.investigation_tasks import (
    InvestigationTaskEventRow,
)

_TERMINAL_SESSION_STATES = ("closed", "lost", "failed")
_ORPHAN_SESSION_STATES = ("provisioning", "ready")
_HOST_RELEASE_EVENT_PREFIX = "host-release:"

_CLOSE_OUTCOME_STATES = {"closed": AgentSessionState.CLOSED, "lost": AgentSessionState.LOST}


def _session_from_row(row: AgentSessionRow) -> AgentSessionV1:
    return AgentSessionV1(
        id=row.id,
        investigation_id=row.investigation_id,
        task_id=row.task_id,
        adapter_kind=OperatorAdapterKind(row.adapter_kind),
        host_session_ref=row.host_session_ref,
        state=AgentSessionState(row.state),
        state_version=row.state_version,
        capability_probe=row.capability_probe,
        capability_sha256=row.capability_sha256,
        probed_at=row.probed_at,
        handoff_id=row.handoff_id,
        task_lease_id=row.task_lease_id,
        task_lease_fence=row.task_lease_fence,
        checkpoint_ref=row.checkpoint_ref,
        checkpoint_sha256=row.checkpoint_sha256,
        reconciliation=row.reconciliation,
        reconciliation_sha256=row.reconciliation_sha256,
        auth_secret_nonce=row.auth_secret_nonce,
        auth_secret_hash=row.auth_secret_hash,
        auth_secret_issued_at=row.auth_secret_issued_at,
        last_activity_at=row.last_activity_at,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        close_reason=row.close_reason,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _session_values(session: AgentSessionV1) -> dict[str, Any]:
    return {
        "id": session.id,
        "investigation_id": session.investigation_id,
        "task_id": session.task_id,
        "adapter_kind": session.adapter_kind.value,
        "host_session_ref": session.host_session_ref,
        "state": session.state.value,
        "state_version": session.state_version,
        "capability_probe": thaw_json(session.capability_probe),
        "capability_sha256": session.capability_sha256,
        "probed_at": session.probed_at,
        "handoff_id": session.handoff_id,
        "task_lease_id": session.task_lease_id,
        "task_lease_fence": session.task_lease_fence,
        "checkpoint_ref": session.checkpoint_ref,
        "checkpoint_sha256": session.checkpoint_sha256,
        "reconciliation": (
            thaw_json(session.reconciliation) if session.reconciliation is not None else None
        ),
        "reconciliation_sha256": session.reconciliation_sha256,
        "auth_secret_nonce": session.auth_secret_nonce,
        "auth_secret_hash": session.auth_secret_hash,
        "auth_secret_issued_at": session.auth_secret_issued_at,
        "last_activity_at": session.last_activity_at,
        "opened_at": session.opened_at,
        "closed_at": session.closed_at,
        "close_reason": session.close_reason,
        "idempotency_key": session.idempotency_key,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _session_mutable_values(session: AgentSessionV1) -> dict[str, Any]:
    """Columns ``update_state`` may change; identity and secret stay frozen."""
    return {
        "host_session_ref": session.host_session_ref,
        "state": session.state.value,
        "state_version": session.state_version,
        "capability_probe": thaw_json(session.capability_probe),
        "capability_sha256": session.capability_sha256,
        "probed_at": session.probed_at,
        "handoff_id": session.handoff_id,
        "task_lease_id": session.task_lease_id,
        "task_lease_fence": session.task_lease_fence,
        "checkpoint_ref": session.checkpoint_ref,
        "checkpoint_sha256": session.checkpoint_sha256,
        "reconciliation": (
            thaw_json(session.reconciliation) if session.reconciliation is not None else None
        ),
        "reconciliation_sha256": session.reconciliation_sha256,
        "last_activity_at": session.last_activity_at,
        "closed_at": session.closed_at,
        "close_reason": session.close_reason,
        "updated_at": session.updated_at,
    }


def _instruction_from_row(row: AgentSessionInstructionRow) -> AgentSessionInstructionV1:
    return AgentSessionInstructionV1(
        id=row.id,
        investigation_id=row.investigation_id,
        task_id=row.task_id,
        session_id=row.session_id,
        idempotency_key=row.idempotency_key,
        text=row.body,
        text_sha256=row.body_sha256,
        state=InstructionState(row.state),
        created_by_kind=row.created_by_actor_kind,
        created_by_id=row.created_by_actor_id,
        queued_at=row.queued_at,
        delivered_at=row.delivered_at,
        acknowledged_at=row.acknowledged_at,
        delivery_attempts=row.delivery_attempts,
        delivery_token=row.delivery_token,
        delivery_mode=(
            InstructionDeliveryMode(row.delivery_mode) if row.delivery_mode is not None else None
        ),
        delivery_sha256=row.delivery_sha256,
        delivery_note_diffs=(
            tuple(row.delivery_note_diffs) if row.delivery_note_diffs is not None else None
        ),
        delivery_attempted_at=row.delivery_attempted_at,
        delivery_unknown_at=row.delivery_unknown_at,
        last_delivery_error=row.last_delivery_error,
        cancelled_at=row.cancelled_at,
        cancel_reason=row.cancel_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _instruction_delivery_values(instruction: AgentSessionInstructionV1) -> dict[str, Any]:
    """Columns a delivery transition may change; identity and body stay frozen."""
    return {
        "state": instruction.state.value,
        "delivered_at": instruction.delivered_at,
        "acknowledged_at": instruction.acknowledged_at,
        "delivery_attempts": instruction.delivery_attempts,
        "delivery_token": instruction.delivery_token,
        "delivery_mode": (
            instruction.delivery_mode.value if instruction.delivery_mode is not None else None
        ),
        "delivery_sha256": instruction.delivery_sha256,
        "delivery_note_diffs": (
            thaw_json(instruction.delivery_note_diffs)
            if instruction.delivery_note_diffs is not None
            else None
        ),
        "delivery_attempted_at": instruction.delivery_attempted_at,
        "delivery_unknown_at": instruction.delivery_unknown_at,
        "last_delivery_error": instruction.last_delivery_error,
        "cancelled_at": instruction.cancelled_at,
        "cancel_reason": instruction.cancel_reason,
        "updated_at": instruction.updated_at,
    }


def _instruction_values(instruction: AgentSessionInstructionV1) -> dict[str, Any]:
    return {
        "id": instruction.id,
        "investigation_id": instruction.investigation_id,
        "task_id": instruction.task_id,
        "session_id": instruction.session_id,
        "idempotency_key": instruction.idempotency_key,
        "body": instruction.text,
        "body_sha256": instruction.text_sha256,
        "created_by_actor_kind": instruction.created_by_kind,
        "created_by_actor_id": instruction.created_by_id,
        "queued_at": instruction.queued_at,
        "created_at": instruction.created_at,
        **_instruction_delivery_values(instruction),
    }


class SqlAlchemyAgentSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, session_id: UUID) -> AgentSessionV1 | None:
        row = await self._session.get(AgentSessionRow, session_id)
        return _session_from_row(row) if row is not None else None

    async def get_by_idempotency(
        self, task_id: UUID, idempotency_key: str
    ) -> AgentSessionV1 | None:
        row = await self._session.scalar(
            select(AgentSessionRow).where(
                AgentSessionRow.task_id == task_id,
                AgentSessionRow.idempotency_key == idempotency_key,
            )
        )
        return _session_from_row(row) if row is not None else None

    async def list_for_investigation(
        self, investigation_id: UUID, *, task_id: UUID | None = None
    ) -> tuple[AgentSessionV1, ...]:
        statement = select(AgentSessionRow).where(
            AgentSessionRow.investigation_id == investigation_id
        )
        if task_id is not None:
            statement = statement.where(AgentSessionRow.task_id == task_id)
        rows = await self._session.scalars(
            statement.order_by(AgentSessionRow.created_at, AgentSessionRow.id)
        )
        return tuple(_session_from_row(row) for row in rows)

    async def list_for_task(self, task_id: UUID) -> tuple[AgentSessionV1, ...]:
        rows = await self._session.scalars(
            select(AgentSessionRow)
            .where(AgentSessionRow.task_id == task_id)
            .order_by(AgentSessionRow.created_at, AgentSessionRow.id)
        )
        return tuple(_session_from_row(row) for row in rows)

    async def list_orphan_sessions(
        self, *, grace_seconds: int, limit: int
    ) -> tuple[AgentSessionV1, ...]:
        """Sessions a crash left ``provisioning``/``ready`` without any lease.

        The age boundary is the database clock (``statement_timestamp()``), so
        a skewed backend clock cannot fail a legitimately in-flight open.
        """
        cutoff = func.statement_timestamp() - func.make_interval(
            0, 0, 0, 0, 0, 0, cast(grace_seconds, Float)
        )
        rows = await self._session.scalars(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.state.in_(_ORPHAN_SESSION_STATES),
                AgentSessionRow.task_lease_id.is_(None),
                AgentSessionRow.created_at <= cutoff,
            )
            .order_by(AgentSessionRow.created_at, AgentSessionRow.id)
            .limit(limit)
        )
        return tuple(_session_from_row(row) for row in rows)

    async def list_pending_host_release_sessions(
        self, *, grace_seconds: int, limit: int
    ) -> tuple[AgentSessionV1, ...]:
        """Terminal sessions whose host teardown has no durable confirmation.

        ``host-release:<session_id>`` is appended by the release service once
        the host stopped; without it the effect is pending (crash between the
        DB commit and the flush).  An already marked ``reconciliation`` is a
        recorded outcome and is never retried automatically.

        The age boundary is ``COALESCE(closed_at, updated_at)``: the D7
        domain (and the 0023 CHECK) sets ``closed_at`` exactly in
        ``closed|failed``, so a ``lost`` session has none and its transition
        time is ``updated_at``.  Filtering on ``closed_at`` alone silently
        excluded every lost session from the teardown recovery, leaving its
        CLI and provider credential alive after a crash (R4-F3).
        """
        cutoff = func.statement_timestamp() - func.make_interval(
            0, 0, 0, 0, 0, 0, cast(grace_seconds, Float)
        )
        released_at = func.coalesce(AgentSessionRow.closed_at, AgentSessionRow.updated_at)
        confirmed = (
            select(InvestigationTaskEventRow.id)
            .where(
                InvestigationTaskEventRow.task_id == AgentSessionRow.task_id,
                InvestigationTaskEventRow.idempotency_key
                == literal(_HOST_RELEASE_EVENT_PREFIX) + cast(AgentSessionRow.id, String),
            )
            .exists()
        )
        rows = await self._session.scalars(
            select(AgentSessionRow)
            .where(
                AgentSessionRow.state.in_(_TERMINAL_SESSION_STATES),
                AgentSessionRow.host_session_ref.is_not(None),
                AgentSessionRow.reconciliation.is_(None),
                released_at <= cutoff,
                ~confirmed,
            )
            .order_by(released_at, AgentSessionRow.id)
            .limit(limit)
        )
        return tuple(_session_from_row(row) for row in rows)

    async def add(self, session: AgentSessionV1) -> tuple[AgentSessionV1, bool]:
        result = await self._session.execute(
            insert(AgentSessionRow)
            .values(**_session_values(session))
            .on_conflict_do_nothing()
            .returning(AgentSessionRow.id)
        )
        if result.scalar_one_or_none() is not None:
            return session, True
        existing = await self.get_by_idempotency(session.task_id, session.idempotency_key)
        if existing is not None:
            if (
                existing.adapter_kind is not session.adapter_kind
                or existing.handoff_id != session.handoff_id
                or existing.capability_sha256 != session.capability_sha256
            ):
                raise VersionConflictError(
                    "Agent session idempotency key records a different request"
                )
            return existing, False
        raise VersionConflictError("The task already has an active agent session")

    async def update_state(
        self, session: AgentSessionV1, *, expected_state_version: int
    ) -> AgentSessionV1 | None:
        if session.state_version != expected_state_version + 1:
            raise ValueError("A session update increments state_version exactly once")
        result = await self._session.execute(
            update(AgentSessionRow)
            .where(
                AgentSessionRow.id == session.id,
                AgentSessionRow.state_version == expected_state_version,
            )
            .values(**_session_mutable_values(session))
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            return None
        await self._session.flush()
        return session

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        row = await self._session.scalar(
            select(AgentSessionRow.state).where(
                AgentSessionRow.id == session_id,
                AgentSessionRow.investigation_id == investigation_id,
                AgentSessionRow.task_id == task_id,
            )
        )
        return row is not None and AgentSessionState(row) in ACTIVE_AGENT_SESSION_STATES

    async def close_session(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        outcome: str,
        reason: str,
    ) -> None:
        target = _CLOSE_OUTCOME_STATES.get(outcome)
        if target is None:
            raise ValueError("close_session outcome must be 'closed' or 'lost'")
        current = await self.get(session_id)
        if (
            current is None
            or current.investigation_id != investigation_id
            or current.task_id != task_id
        ):
            raise VersionConflictError("The task lease names no bound agent session")
        if current.is_terminal or current.state is AgentSessionState.LOST:
            return
        if target is AgentSessionState.CLOSED and not current.is_active:
            target = AgentSessionState.LOST
        try:
            updated = current.with_state(target, now=datetime.now(UTC), close_reason=reason)
        except SessionTransitionError:
            updated = current.with_state(
                AgentSessionState.LOST, now=datetime.now(UTC), close_reason=reason
            )
        stored = await self.update_state(updated, expected_state_version=current.state_version)
        if stored is None:
            # A concurrent transition won; a terminal session is already closed.
            refreshed = await self.get(session_id)
            if refreshed is not None and (
                refreshed.is_terminal or refreshed.state is AgentSessionState.LOST
            ):
                return
            raise VersionConflictError("The agent session changed while closing it")

    async def add_instruction(
        self, instruction: AgentSessionInstructionV1
    ) -> tuple[AgentSessionInstructionV1, bool]:
        result = await self._session.execute(
            insert(AgentSessionInstructionRow)
            .values(**_instruction_values(instruction))
            .on_conflict_do_nothing()
            .returning(AgentSessionInstructionRow.id)
        )
        if result.scalar_one_or_none() is not None:
            return instruction, True
        existing = await self.get_instruction_by_idempotency(
            instruction.session_id, instruction.idempotency_key
        )
        if existing is not None:
            if existing.text_sha256 != instruction.text_sha256:
                raise VersionConflictError("Instruction idempotency key records a different body")
            return existing, False
        raise VersionConflictError("The instruction cannot be queued for this session")

    async def get_instruction(self, instruction_id: UUID) -> AgentSessionInstructionV1 | None:
        row = await self._session.get(AgentSessionInstructionRow, instruction_id)
        return _instruction_from_row(row) if row is not None else None

    async def get_instruction_by_idempotency(
        self, session_id: UUID, idempotency_key: str
    ) -> AgentSessionInstructionV1 | None:
        row = await self._session.scalar(
            select(AgentSessionInstructionRow).where(
                AgentSessionInstructionRow.session_id == session_id,
                AgentSessionInstructionRow.idempotency_key == idempotency_key,
            )
        )
        return _instruction_from_row(row) if row is not None else None

    async def list_instructions(self, session_id: UUID) -> tuple[AgentSessionInstructionV1, ...]:
        rows = await self._session.scalars(
            select(AgentSessionInstructionRow)
            .where(AgentSessionInstructionRow.session_id == session_id)
            .order_by(AgentSessionInstructionRow.queued_at, AgentSessionInstructionRow.id)
        )
        return tuple(_instruction_from_row(row) for row in rows)

    async def update_instruction(
        self,
        instruction: AgentSessionInstructionV1,
        *,
        expected_state: InstructionState,
    ) -> AgentSessionInstructionV1 | None:
        """Legacy state CAS (``None`` on a lost race); ID-2 uses the delivery CAS."""
        result = await self._session.execute(
            update(AgentSessionInstructionRow)
            .where(
                AgentSessionInstructionRow.id == instruction.id,
                AgentSessionInstructionRow.state == expected_state.value,
            )
            .values(**_instruction_delivery_values(instruction))
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            return None
        await self._session.flush()
        return instruction

    async def get_instruction_by_token(
        self,
        instruction_id: UUID,
        delivery_token: UUID,
        *,
        state: InstructionState | None = None,
    ) -> AgentSessionInstructionV1 | None:
        """Reload one attempt by id + token (phase C, F3-D §3.3)."""
        statement = select(AgentSessionInstructionRow).where(
            AgentSessionInstructionRow.id == instruction_id,
            AgentSessionInstructionRow.delivery_token == delivery_token,
        )
        if state is not None:
            statement = statement.where(AgentSessionInstructionRow.state == state.value)
        row = await self._session.scalar(statement)
        return _instruction_from_row(row) if row is not None else None

    async def list_pending_instructions(
        self, *, limit: int
    ) -> tuple[AgentSessionInstructionV1, ...]:
        """Drain candidates of *active* sessions in queue order (F3-D §3.3).

        The design's candidate is "an active session carrying an instruction
        ``queued|delivering|delivery_unknown``": rows left by a terminal
        session can never be delivered and must not occupy the bounded batch,
        or 50 dead rows would starve every live instruction behind them
        (R4-F2).  The partial ``ix_agent_session_instructions_pending`` index
        still drives the scan; the join is on the session primary key.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        rows = await self._session.scalars(
            select(AgentSessionInstructionRow)
            .join(
                AgentSessionRow,
                AgentSessionRow.id == AgentSessionInstructionRow.session_id,
            )
            .where(
                AgentSessionInstructionRow.state.in_(
                    tuple(state.value for state in INSTRUCTION_PENDING_STATES)
                ),
                AgentSessionRow.state.in_(
                    tuple(state.value for state in ACTIVE_AGENT_SESSION_STATES)
                ),
            )
            .order_by(
                AgentSessionInstructionRow.queued_at,
                AgentSessionInstructionRow.id,
            )
            .limit(limit)
        )
        return tuple(_instruction_from_row(row) for row in rows)

    async def list_stale_delivering(
        self, *, attempted_before: datetime, limit: int
    ) -> tuple[AgentSessionInstructionV1, ...]:
        """Stale phase-A attempts across ALL sessions, oldest first (R4B-F1).

        The delivery batch is bounded to active sessions so dead rows cannot
        starve it; reconciliation must not reuse that list, or a crash whose
        session later became ``lost`` would pin the instruction in
        ``delivering`` for ever (cancel and transfer both refuse that state).
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        attempted = func.coalesce(
            AgentSessionInstructionRow.delivery_attempted_at,
            AgentSessionInstructionRow.updated_at,
        )
        rows = await self._session.scalars(
            select(AgentSessionInstructionRow)
            .where(
                AgentSessionInstructionRow.state == InstructionState.DELIVERING.value,
                attempted <= attempted_before,
            )
            .order_by(attempted, AgentSessionInstructionRow.id)
            .limit(limit)
        )
        return tuple(_instruction_from_row(row) for row in rows)

    async def update_instruction_delivery(
        self,
        instruction: AgentSessionInstructionV1,
        *,
        expected_state: InstructionState,
        expected_delivery_token: UUID | None,
    ) -> AgentSessionInstructionV1:
        """CAS one delivery transition on (id, state, token).

        The token is part of the expectation so two drainers, or a drainer and
        an acknowledgement, can never overwrite each other's attempt.  A lost
        race raises the existing version conflict instead of returning ``None``.
        """
        result = await self._session.execute(
            update(AgentSessionInstructionRow)
            .where(
                AgentSessionInstructionRow.id == instruction.id,
                AgentSessionInstructionRow.state == expected_state.value,
                AgentSessionInstructionRow.delivery_token.is_not_distinct_from(
                    expected_delivery_token
                ),
            )
            .values(**_instruction_delivery_values(instruction))
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise VersionConflictError("The instruction delivery attempt changed concurrently")
        await self._session.flush()
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
        """Cancel a never-delivered instruction with its bounded human reason.

        Only ``queued`` and ``delivery_unknown`` are cancellable; a
        ``delivering`` attempt must first resolve to ``delivered`` or
        ``delivery_unknown`` (F3-D §3.5).
        """
        current = await self.get_instruction(instruction_id)
        if (
            current is None
            or current.state is not expected_state
            or current.delivery_token != expected_delivery_token
        ):
            raise VersionConflictError("The instruction delivery attempt changed concurrently")
        cancelled = current.with_cancellation(reason=reason, now=now)
        return await self.update_instruction_delivery(
            cancelled,
            expected_state=expected_state,
            expected_delivery_token=expected_delivery_token,
        )


__all__ = ["SqlAlchemyAgentSessionRepository"]

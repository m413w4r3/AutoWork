"""Agentic operator port contracts and the technical session state machine.

ADR-0011 D7 makes one typed port shared by the Codex CLI and Claude Code
adapters.  The port is deliberately narrow: a *real* capability probe, the
session lifecycle, instruction delivery, interruption, resumption, result
collection and typed reconciliation.  The business layer never reads CLI
internals; adapters report facts, and this module refuses facts that no probe
established (the ``modes`` of an :class:`OperatorCapabilitiesV1` must all be
advertised by a recorded probe command).

The session aggregate mirrors ``agent_sessions`` (ADR-0011 D2) one check for
one check.  Its technical state is the sole authority for the session
(``state``/``state_version``), never the task business status; every mutation
is a compare-and-swap on ``state_version`` performed by the application
service.  A session in a terminal state is final, and ``closed_at`` exists
exactly for ``closed``/``failed`` as the schema enforces.

Session authentication material is never logged: the aggregate keeps the
nonce and the secret hash out of ``repr`` and out of :meth:`to_payload`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cti_app.domain.errors import DomainError
from cti_app.domain.investigation import canonical_json_bytes, freeze_json, thaw_json

OPERATOR_CAPABILITIES_SCHEMA_VERSION: str = "agentic-operator-capabilities.v1"
AGENT_SESSION_SCHEMA_VERSION = "agent-session.v1"
INSTRUCTION_SCHEMA_VERSION = "agent-session-instruction.v1"

AGENT_SESSION_SECRET_NONCE_BYTES = 32
# Queue/domain bound.  The host delivery capacity is narrower: its 32 KiB
# instruction block includes the fixed lead-in header (INSTRUCTION-DELIVERY
# §3.4, ``HOST_MAX_INSTRUCTION_BYTES``).  A longer text is accepted and stays
# cancellable here; delivery refuses it visibly
# (``instruction_text_exceeds_host_bound``) instead of truncating it.
INSTRUCTION_MAX_TEXT_BYTES = 64 * 1024
RESULT_MAX_TEXT_BYTES = 64 * 1024
RESULT_MAX_SUMMARY_CHARS = 4096

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class AgenticOperatorError(DomainError):
    """Base refusal for an invalid port contract or session state."""

    code = "agentic_operator_invalid"


class SessionTransitionError(AgenticOperatorError):
    """The (from, to) session-state pair is absent from the D7 machine."""

    code = "agent_session_transition_invalid"


class AgentSessionStateError(AgenticOperatorError):
    """A persisted session or instruction violates an ADR-0011 D2 invariant."""

    code = "agent_session_state_invalid"


class OperatorAdapterKind(StrEnum):
    CODEX_CLI = "codex_cli"
    CLAUDE_CODE = "claude_code"


class AgentSessionState(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    RUNNING = "running"
    AWAITING_INSTRUCTION = "awaiting_instruction"
    INTERRUPT_REQUESTED = "interrupt_requested"
    STOPPED = "stopped"
    LOST = "lost"
    RECONCILING = "reconciling"
    CLOSED = "closed"
    FAILED = "failed"


# The D2 partial unique index covers exactly these states: one active session
# per task.  ``lost``/``stopped`` are not active, so a new session may follow a
# reconciled loss.
ACTIVE_AGENT_SESSION_STATES = frozenset(
    {
        AgentSessionState.PROVISIONING,
        AgentSessionState.READY,
        AgentSessionState.RUNNING,
        AgentSessionState.AWAITING_INSTRUCTION,
        AgentSessionState.INTERRUPT_REQUESTED,
        AgentSessionState.RECONCILING,
    }
)

# ADR-0011 D2: ``closed_at`` is required in closed/failed (and only there).
CLOSED_AT_REQUIRED_SESSION_STATES = frozenset({AgentSessionState.CLOSED, AgentSessionState.FAILED})

TERMINAL_AGENT_SESSION_STATES = frozenset({AgentSessionState.CLOSED, AgentSessionState.FAILED})

RESUMABLE_AGENT_SESSION_STATES = frozenset({AgentSessionState.LOST, AgentSessionState.STOPPED})


class SessionReconcileOutcome(StrEnum):
    SESSION_ALIVE = "session_alive"
    TERMINATED_CLEAN = "terminated_clean"
    LOST_WITH_HANDOFF = "lost_with_handoff"
    LOST_UNRECONCILED = "lost_unreconciled"
    RESUME_AVAILABLE = "resume_available"
    RESUME_UNAVAILABLE = "resume_unavailable"


# D7: reconciliation never relaunches.  An alive session keeps its state, a
# clean termination closes it, and every lost/resume outcome records ``lost``;
# only an explicitly resumable loss can later be resumed by a human decision.
RECONCILE_OUTCOME_STATE: Mapping[SessionReconcileOutcome, AgentSessionState | None] = {
    SessionReconcileOutcome.SESSION_ALIVE: None,
    SessionReconcileOutcome.TERMINATED_CLEAN: AgentSessionState.CLOSED,
    SessionReconcileOutcome.LOST_WITH_HANDOFF: AgentSessionState.LOST,
    SessionReconcileOutcome.LOST_UNRECONCILED: AgentSessionState.LOST,
    SessionReconcileOutcome.RESUME_AVAILABLE: AgentSessionState.LOST,
    SessionReconcileOutcome.RESUME_UNAVAILABLE: AgentSessionState.LOST,
}

RECONCILE_OUTCOMES_WITH_HANDOFF = frozenset({SessionReconcileOutcome.LOST_WITH_HANDOFF})
RECONCILE_OUTCOMES_RESUMABLE = frozenset({SessionReconcileOutcome.RESUME_AVAILABLE})


class InstructionState(StrEnum):
    QUEUED = "queued"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    DELIVERY_UNKNOWN = "delivery_unknown"
    CANCELLED = "cancelled"


class InstructionDeliveryMode(StrEnum):
    """How a proven delivery reached the operator (F3-D §3.4)."""

    LIVE = "live"
    NEXT_RUN = "next_run"


# F3-D §3.2.  ``acknowledged``/``cancelled`` are terminal; ``delivered`` is
# never cancelled and ``queued`` is neither delivered nor acknowledged
# directly: only a proven host effect advances the queue to ``delivered``.
INSTRUCTION_STATE_TRANSITIONS: Mapping[InstructionState, frozenset[InstructionState]] = {
    InstructionState.QUEUED: frozenset(
        {
            InstructionState.DELIVERING,
            InstructionState.CANCELLED,
        }
    ),
    InstructionState.DELIVERING: frozenset(
        {
            InstructionState.DELIVERED,
            InstructionState.QUEUED,
            InstructionState.DELIVERY_UNKNOWN,
        }
    ),
    InstructionState.DELIVERED: frozenset({InstructionState.ACKNOWLEDGED}),
    InstructionState.DELIVERY_UNKNOWN: frozenset(
        {
            InstructionState.DELIVERED,
            InstructionState.QUEUED,
            InstructionState.CANCELLED,
        }
    ),
    InstructionState.ACKNOWLEDGED: frozenset(),
    InstructionState.CANCELLED: frozenset(),
}

# The drain's candidate set; it matches the partial index 0027 creates.
INSTRUCTION_PENDING_STATES = frozenset(
    {
        InstructionState.QUEUED,
        InstructionState.DELIVERING,
        InstructionState.DELIVERY_UNKNOWN,
    }
)
TERMINAL_INSTRUCTION_STATES = frozenset({InstructionState.ACKNOWLEDGED, InstructionState.CANCELLED})


class InstructionDeliveryOutcome(StrEnum):
    ACCEPTED = "accepted"
    BUSY = "busy"
    REFUSED = "refused"


SESSION_STATE_TRANSITIONS: Mapping[AgentSessionState, frozenset[AgentSessionState]] = {
    AgentSessionState.PROVISIONING: frozenset(
        {
            AgentSessionState.READY,
            AgentSessionState.RECONCILING,
            AgentSessionState.FAILED,
            AgentSessionState.LOST,
        }
    ),
    AgentSessionState.READY: frozenset(
        {
            AgentSessionState.RUNNING,
            AgentSessionState.RECONCILING,
            AgentSessionState.STOPPED,
            AgentSessionState.LOST,
            AgentSessionState.FAILED,
            AgentSessionState.CLOSED,
        }
    ),
    AgentSessionState.RUNNING: frozenset(
        {
            AgentSessionState.AWAITING_INSTRUCTION,
            AgentSessionState.INTERRUPT_REQUESTED,
            AgentSessionState.STOPPED,
            AgentSessionState.LOST,
            AgentSessionState.RECONCILING,
            AgentSessionState.FAILED,
            AgentSessionState.CLOSED,
        }
    ),
    AgentSessionState.AWAITING_INSTRUCTION: frozenset(
        {
            AgentSessionState.RUNNING,
            AgentSessionState.INTERRUPT_REQUESTED,
            AgentSessionState.STOPPED,
            AgentSessionState.LOST,
            AgentSessionState.RECONCILING,
            AgentSessionState.FAILED,
            AgentSessionState.CLOSED,
        }
    ),
    AgentSessionState.INTERRUPT_REQUESTED: frozenset(
        {
            AgentSessionState.RECONCILING,
            AgentSessionState.STOPPED,
            AgentSessionState.RUNNING,
            AgentSessionState.LOST,
            AgentSessionState.FAILED,
            AgentSessionState.CLOSED,
        }
    ),
    AgentSessionState.STOPPED: frozenset(
        {
            AgentSessionState.READY,
            AgentSessionState.LOST,
            AgentSessionState.FAILED,
            AgentSessionState.CLOSED,
        }
    ),
    AgentSessionState.LOST: frozenset(
        {
            AgentSessionState.RECONCILING,
            AgentSessionState.READY,
            AgentSessionState.FAILED,
            AgentSessionState.CLOSED,
        }
    ),
    AgentSessionState.RECONCILING: frozenset(
        {
            AgentSessionState.PROVISIONING,
            AgentSessionState.READY,
            AgentSessionState.RUNNING,
            AgentSessionState.AWAITING_INSTRUCTION,
            AgentSessionState.INTERRUPT_REQUESTED,
            AgentSessionState.STOPPED,
            AgentSessionState.LOST,
            AgentSessionState.FAILED,
            AgentSessionState.CLOSED,
        }
    ),
    AgentSessionState.CLOSED: frozenset(),
    AgentSessionState.FAILED: frozenset(),
}


def validate_session_transition(from_state: AgentSessionState, to_state: AgentSessionState) -> None:
    """Refuse any pair the D7 session machine does not declare."""
    if not isinstance(from_state, AgentSessionState):
        raise TypeError("from_state must use AgentSessionState")
    if not isinstance(to_state, AgentSessionState):
        raise TypeError("to_state must use AgentSessionState")
    if to_state not in SESSION_STATE_TRANSITIONS[from_state]:
        raise SessionTransitionError(
            f"{from_state.value} -> {to_state.value} is not a session transition"
        )


def validate_instruction_transition(
    from_state: InstructionState, to_state: InstructionState
) -> None:
    if not isinstance(from_state, InstructionState):
        raise TypeError("from_state must use InstructionState")
    if not isinstance(to_state, InstructionState):
        raise TypeError("to_state must use InstructionState")
    if to_state not in INSTRUCTION_STATE_TRANSITIONS[from_state]:
        raise SessionTransitionError(
            f"instruction {from_state.value} -> {to_state.value} is not a transition"
        )


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _sha256_hex(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest") from exc
    if value.lower() != value:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _unique_modes(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise TypeError(f"{name} entries must be strings")
        mode = raw.strip()
        if not mode or len(mode) > 64 or "\x00" in mode:
            raise ValueError(f"{name} entries must be non-empty, bounded mode names")
        if mode in seen:
            raise ValueError(f"{name} must not repeat a mode")
        seen.add(mode)
        normalized.append(mode)
    return tuple(normalized)


class OperatorProbeCommand(_WireModel):
    """One real command the adapter ran while probing its capabilities.

    ``advertised_modes`` is what the command's output actually reported; a
    capability object can never claim a mode absent from every probe.
    """

    argv: tuple[str, ...] = Field(min_length=1)
    exit_code: int = Field(ge=0)
    stdout_sha256: str
    advertised_modes: tuple[str, ...] = ()
    resolved_version: str | None = Field(default=None, max_length=256)

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("argv entries must be non-empty and NUL-free")
        return value

    @field_validator("stdout_sha256")
    @classmethod
    def _validate_stdout_sha256(cls, value: str) -> str:
        return _sha256_hex(value, "stdout_sha256")

    @field_validator("advertised_modes")
    @classmethod
    def _validate_modes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_modes(value, "advertised_modes")


class OperatorConfinementV1(_WireModel):
    """What the host actually attested for the reasoning-session sandbox.

    ``network_mode`` is ``None`` only for a profile that does not run a real CLI
    (the fake test profile); a real provider session must attest
    ``isolated_egress`` with ``loopback_isolated`` and a real network namespace
    before the backend will open it (R03 B1).
    """

    profile: str = Field(min_length=1, max_length=64)
    bwrap_ok: bool
    netns: bool = False
    network_mode: Literal["isolated_egress"] | None = None
    loopback_isolated: bool = False
    reason: str | None = Field(default=None, max_length=512)


class OperatorCapabilitiesV1(_WireModel):
    """Probed capability facts, never invented flags (ADR-0011 D7, D01).

    ``modes`` and ``permission_modes`` must be a subset of the modes recorded
    by the probe commands, so an adapter cannot advertise an untested mode.
    ``capability_sha256`` is derived from the canonical payload; a supplied
    value is verified instead of trusted.
    """

    schema_version: Literal["agentic-operator-capabilities.v1"] = "agentic-operator-capabilities.v1"
    adapter_kind: OperatorAdapterKind
    executable: str = Field(min_length=1, max_length=4096)
    version: str = Field(min_length=1, max_length=256)
    modes: tuple[str, ...] = ()
    supports_resume: bool = False
    supports_structured_output: bool = False
    supports_non_interactive: bool = False
    supports_stop: bool = False
    # F3-D §3.4: the host advertises that one instruction starts a next run
    # carrying its text.  Absent/false keeps the queue undelivered; the flag is
    # additive so an older host probe still validates.
    supports_next_run_instruction: bool = False
    permission_modes: tuple[str, ...] = ()
    confinement: OperatorConfinementV1 | None = None
    probed_at: datetime
    probes: tuple[OperatorProbeCommand, ...] = Field(min_length=1)
    capability_sha256: str = ""

    @field_validator("adapter_kind")
    @classmethod
    def _validate_adapter_kind(cls, value: OperatorAdapterKind) -> OperatorAdapterKind:
        if not isinstance(value, OperatorAdapterKind):
            raise TypeError("adapter_kind must use OperatorAdapterKind")
        return value

    @field_validator("executable")
    @classmethod
    def _validate_executable(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("executable must be a non-empty path")
        return normalized

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("version must not be empty")
        return normalized

    @field_validator("modes")
    @classmethod
    def _validate_modes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_modes(value, "modes")

    @field_validator("permission_modes")
    @classmethod
    def _validate_permission_modes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_modes(value, "permission_modes")

    @field_validator("probed_at")
    @classmethod
    def _validate_probed_at(cls, value: datetime) -> datetime:
        return _aware(value, "probed_at")

    @model_validator(mode="after")
    def _validate_probed_facts(self) -> OperatorCapabilitiesV1:
        advertised: set[str] = set()
        for probe in self.probes:
            advertised.update(probe.advertised_modes)
        unprobed = sorted(set(self.modes) - advertised)
        if unprobed:
            raise ValueError("capability probe reports only probed modes: " + ", ".join(unprobed))
        unprobed_permissions = sorted(set(self.permission_modes) - advertised)
        if unprobed_permissions:
            raise ValueError(
                "capability probe reports only probed permission modes: "
                + ", ".join(unprobed_permissions)
            )
        if any(
            flag
            for flag in (
                self.supports_resume,
                self.supports_structured_output,
                self.supports_non_interactive,
                self.supports_stop,
                self.supports_next_run_instruction,
            )
        ) and not any(probe.exit_code == 0 for probe in self.probes):
            raise ValueError("capability facts require at least one successful probe")
        expected = self.expected_capability_sha256()
        if self.capability_sha256:
            if self.capability_sha256 != expected:
                raise ValueError("capability_sha256 does not match the probed facts")
        else:
            object.__setattr__(self, "capability_sha256", expected)
        return self

    def payload(self) -> dict[str, Any]:
        """The canonical probed payload, hash excluded.

        ``supports_next_run_instruction`` is left out while false: the flag is
        additive (F3-D §3.4), so a probe recorded before the host advertised it
        keeps the SHA-256 it was stored under instead of being re-hashed.
        """
        payload = self.model_dump(mode="json", exclude={"capability_sha256"})
        if not self.supports_next_run_instruction:
            payload.pop("supports_next_run_instruction", None)
        return payload

    def expected_capability_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.payload())).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ProbeCapabilitiesRequest(_WireModel):
    adapter_kind: OperatorAdapterKind
    executable: str = Field(min_length=1, max_length=4096)


class PrepareSessionRequest(_WireModel):
    session_id: UUID
    investigation_id: UUID
    task_id: UUID
    adapter_kind: OperatorAdapterKind
    handoff_id: UUID
    capability_sha256: str
    # The alias the verified human chose at open (D7/§6).  It is a request
    # input, never a probed capability: an adapter passes it to the host
    # context provider, which validates it against the canonical context.
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")

    @field_validator("capability_sha256")
    @classmethod
    def _validate_capability_sha256(cls, value: str) -> str:
        return _sha256_hex(value, "capability_sha256")


class PreparedOperatorSessionV1(_WireModel):
    session_id: UUID
    investigation_id: UUID
    task_id: UUID
    adapter_kind: OperatorAdapterKind
    host_session_ref: str = Field(min_length=1, max_length=1024)
    capabilities: OperatorCapabilitiesV1
    prepared_at: datetime

    @field_validator("host_session_ref")
    @classmethod
    def _validate_host_session_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("host_session_ref must be a non-empty reference")
        return normalized

    @field_validator("prepared_at")
    @classmethod
    def _validate_prepared_at(cls, value: datetime) -> datetime:
        return _aware(value, "prepared_at")

    @model_validator(mode="after")
    def _validate_capabilities(self) -> PreparedOperatorSessionV1:
        if self.capabilities.adapter_kind is not self.adapter_kind:
            raise ValueError("prepared session capabilities belong to another adapter")
        return self


class StartSessionRequest(_WireModel):
    session_id: UUID
    adapter_kind: OperatorAdapterKind
    host_session_ref: str = Field(min_length=1, max_length=1024)
    capability_sha256: str

    @field_validator("capability_sha256")
    @classmethod
    def _validate_capability_sha256(cls, value: str) -> str:
        return _sha256_hex(value, "capability_sha256")


class SessionStatusRequest(_WireModel):
    session_id: UUID
    host_session_ref: str = Field(min_length=1, max_length=1024)


class SessionProcessStatusV1(_WireModel):
    session_id: UUID
    host_session_ref: str = Field(min_length=1, max_length=1024)
    alive: bool
    exit_code: int | None = Field(default=None, ge=0)
    last_activity_at: datetime | None = None
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")

    @field_validator("last_activity_at")
    @classmethod
    def _validate_last_activity_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _aware(value, "last_activity_at")


class SendInstructionRequest(_WireModel):
    session_id: UUID
    host_session_ref: str = Field(min_length=1, max_length=1024)
    instruction_id: UUID
    instruction_sha256: str
    text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=255)

    @field_validator("instruction_sha256")
    @classmethod
    def _validate_instruction_sha256(cls, value: str) -> str:
        return _sha256_hex(value, "instruction_sha256")


class InstructionReceiptV1(_WireModel):
    session_id: UUID
    instruction_id: UUID
    outcome: InstructionDeliveryOutcome
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")


class RequestHandoffRequest(_WireModel):
    session_id: UUID
    host_session_ref: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=4096)


class HandoffRequestReceiptV1(_WireModel):
    session_id: UUID
    checkpoint_ref: str = Field(min_length=1, max_length=1024)
    checkpoint_sha256: str
    requested_at: datetime

    @field_validator("checkpoint_sha256")
    @classmethod
    def _validate_checkpoint_sha256(cls, value: str) -> str:
        return _sha256_hex(value, "checkpoint_sha256")

    @field_validator("requested_at")
    @classmethod
    def _validate_requested_at(cls, value: datetime) -> datetime:
        return _aware(value, "requested_at")


class InterruptSessionRequest(_WireModel):
    session_id: UUID
    host_session_ref: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=4096)


class InterruptSessionResultV1(_WireModel):
    session_id: UUID
    stopped: bool
    detail: str | None = Field(default=None, max_length=4096)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")


class ResumeSessionRequest(_WireModel):
    session_id: UUID
    adapter_kind: OperatorAdapterKind
    host_session_ref: str = Field(min_length=1, max_length=1024)
    capability_sha256: str

    @field_validator("capability_sha256")
    @classmethod
    def _validate_capability_sha256(cls, value: str) -> str:
        return _sha256_hex(value, "capability_sha256")


class ResumeSessionResultV1(_WireModel):
    session_id: UUID
    resumed: bool
    host_session_ref: str | None = Field(default=None, max_length=1024)
    detail: str | None = Field(default=None, max_length=4096)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")

    @model_validator(mode="after")
    def _validate_resumed_ref(self) -> ResumeSessionResultV1:
        if self.resumed and not (self.host_session_ref or "").strip():
            raise ValueError("a resumed session requires its host_session_ref")
        return self


class CollectResultsRequest(_WireModel):
    session_id: UUID
    host_session_ref: str = Field(min_length=1, max_length=1024)
    cursor: str | None = Field(default=None, max_length=1024)


class OperatorResultItemV1(_WireModel):
    result_id: str = Field(min_length=1, max_length=255)
    kind: Literal["note", "report", "artifact", "observation_proposal"]
    summary: str = Field(min_length=1, max_length=RESULT_MAX_SUMMARY_CHARS)
    content_sha256: str | None = None
    body: str | None = None

    @field_validator("content_sha256")
    @classmethod
    def _validate_content_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _sha256_hex(value, "content_sha256")

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value.encode("utf-8")) > RESULT_MAX_TEXT_BYTES:
            raise ValueError("a collected result body is bounded to 64 KiB")
        return value


class OperatorResultsV1(_WireModel):
    session_id: UUID
    items: tuple[OperatorResultItemV1, ...] = ()
    next_cursor: str | None = Field(default=None, max_length=1024)
    truncated: bool = False
    checkpoint_ref: str | None = Field(default=None, max_length=1024)
    checkpoint_sha256: str | None = None
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")

    @field_validator("checkpoint_sha256")
    @classmethod
    def _validate_checkpoint_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _sha256_hex(value, "checkpoint_sha256")

    @model_validator(mode="after")
    def _validate_checkpoint_pair(self) -> OperatorResultsV1:
        if (self.checkpoint_ref is None) != (self.checkpoint_sha256 is None):
            raise ValueError("checkpoint_ref and checkpoint_sha256 form a pair")
        return self


class ReconcileSessionRequest(_WireModel):
    session_id: UUID
    host_session_ref: str | None = Field(default=None, max_length=1024)
    # Internal host request identity derived from the durable session version.
    # The HTTP route does not expose this field.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class SessionReconciliationV1(_WireModel):
    """The typed D7 reconciliation outcome; it never carries a relaunch.

    ``lost_with_handoff`` must cite the checkpoint pair, and a resumable loss
    must name the host session the adapter can actually resume.
    """

    session_id: UUID
    outcome: SessionReconcileOutcome
    host_session_ref: str | None = Field(default=None, max_length=1024)
    checkpoint_ref: str | None = Field(default=None, max_length=1024)
    checkpoint_sha256: str | None = None
    detail: str | None = Field(default=None, max_length=4096)
    observed_at: datetime
    reconciliation_sha256: str = ""

    @field_validator("observed_at")
    @classmethod
    def _validate_observed_at(cls, value: datetime) -> datetime:
        return _aware(value, "observed_at")

    @field_validator("checkpoint_sha256")
    @classmethod
    def _validate_checkpoint_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _sha256_hex(value, "checkpoint_sha256")

    @model_validator(mode="after")
    def _validate_outcome_facts(self) -> SessionReconciliationV1:
        if (self.checkpoint_ref is None) != (self.checkpoint_sha256 is None):
            raise ValueError("checkpoint_ref and checkpoint_sha256 form a pair")
        if self.outcome in RECONCILE_OUTCOMES_WITH_HANDOFF and self.checkpoint_ref is None:
            raise ValueError("lost_with_handoff requires a checkpoint pair")
        if (
            self.outcome in RECONCILE_OUTCOMES_RESUMABLE
            and not (self.host_session_ref or "").strip()
        ):
            raise ValueError("resume_available requires the resumable host_session_ref")
        expected = self.expected_reconciliation_sha256()
        if self.reconciliation_sha256:
            if self.reconciliation_sha256 != expected:
                raise ValueError("reconciliation_sha256 does not match its payload")
        else:
            object.__setattr__(self, "reconciliation_sha256", expected)
        return self

    def payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"reconciliation_sha256"})

    def expected_reconciliation_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.payload())).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class AgenticOperatorPort(Protocol):
    """The shared D7 adapter surface; one implementation per CLI kind."""

    @property
    def adapter_kind(self) -> OperatorAdapterKind: ...

    async def probe_capabilities(
        self, request: ProbeCapabilitiesRequest
    ) -> OperatorCapabilitiesV1: ...

    async def prepare_session(
        self, request: PrepareSessionRequest
    ) -> PreparedOperatorSessionV1: ...

    async def set_tool_manifest(
        self, session_id: UUID, *, host_session_ref: str, capability_sha256: str
    ) -> None:
        """Post the closed tool manifest the host accepts before any run."""
        ...

    async def start_session(self, request: StartSessionRequest) -> SessionProcessStatusV1: ...

    async def get_session_status(self, request: SessionStatusRequest) -> SessionProcessStatusV1: ...

    async def send_instruction(self, request: SendInstructionRequest) -> InstructionReceiptV1: ...

    async def request_handoff(self, request: RequestHandoffRequest) -> HandoffRequestReceiptV1: ...

    async def interrupt_session(
        self, request: InterruptSessionRequest
    ) -> InterruptSessionResultV1: ...

    async def resume_session(self, request: ResumeSessionRequest) -> ResumeSessionResultV1: ...

    async def collect_results(self, request: CollectResultsRequest) -> OperatorResultsV1: ...

    async def reconcile_session(
        self, request: ReconcileSessionRequest
    ) -> SessionReconciliationV1: ...


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    frozen = freeze_json(value)
    return cast(Mapping[str, Any], frozen)


def _optional_pair(left: object, right: object, left_name: str, right_name: str) -> None:
    if (left is None) != (right is None):
        raise AgentSessionStateError(f"{left_name} and {right_name} form a pair")


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSessionV1:
    """One technical operator session, exactly as ADR-0011 D2 sketches it.

    Authentication material (nonce, hash) never appears in ``repr``; the
    ``capability_probe`` payload is revalidated against its stored SHA-256 so a
    hydrated row and an in-memory object enforce the same facts.
    """

    investigation_id: UUID
    task_id: UUID
    adapter_kind: OperatorAdapterKind
    state: AgentSessionState
    state_version: int
    capability_probe: Mapping[str, Any]
    capability_sha256: str
    probed_at: datetime
    auth_secret_nonce: bytes = field(repr=False)
    auth_secret_hash: str = field(repr=False)
    auth_secret_issued_at: datetime
    opened_at: datetime
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    id: UUID = field(default_factory=uuid4)
    host_session_ref: str | None = None
    handoff_id: UUID | None = None
    task_lease_id: UUID | None = None
    task_lease_fence: int | None = None
    checkpoint_ref: str | None = None
    checkpoint_sha256: str | None = None
    reconciliation: Mapping[str, Any] | None = None
    reconciliation_sha256: str | None = None
    last_activity_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise TypeError("id must be a UUID")
        if not isinstance(self.investigation_id, UUID):
            raise TypeError("investigation_id must be a UUID")
        if not isinstance(self.task_id, UUID):
            raise TypeError("task_id must be a UUID")
        if not isinstance(self.adapter_kind, OperatorAdapterKind):
            raise TypeError("adapter_kind must use OperatorAdapterKind")
        if not isinstance(self.state, AgentSessionState):
            raise TypeError("state must use AgentSessionState")
        if isinstance(self.state_version, bool) or not isinstance(self.state_version, int):
            raise TypeError("state_version must be an integer")
        if self.state_version < 1:
            raise AgentSessionStateError("state_version must be >= 1")
        if self.handoff_id is not None and not isinstance(self.handoff_id, UUID):
            raise TypeError("handoff_id must be a UUID or None")
        if self.host_session_ref is not None:
            if not isinstance(self.host_session_ref, str) or not self.host_session_ref.strip():
                raise ValueError("host_session_ref must not be empty")
        object.__setattr__(
            self, "capability_probe", _mapping(self.capability_probe, "capability_probe")
        )
        capabilities = OperatorCapabilitiesV1.model_validate(self.capability_probe)
        if capabilities.adapter_kind is not self.adapter_kind:
            raise AgentSessionStateError("capability probe belongs to another adapter")
        _sha256_hex(self.capability_sha256, "capability_sha256")
        if capabilities.capability_sha256 != self.capability_sha256:
            raise AgentSessionStateError("capability probe hash disagrees with its row")
        _aware(self.probed_at, "probed_at")
        if capabilities.probed_at != self.probed_at:
            raise AgentSessionStateError("probed_at disagrees with the capability probe")
        if not isinstance(self.auth_secret_nonce, bytes):
            raise TypeError("auth_secret_nonce must be bytes")
        if len(self.auth_secret_nonce) != AGENT_SESSION_SECRET_NONCE_BYTES:
            raise AgentSessionStateError("auth_secret_nonce must be 32 bytes")
        _sha256_hex(self.auth_secret_hash, "auth_secret_hash")
        _aware(self.auth_secret_issued_at, "auth_secret_issued_at")
        _aware(self.opened_at, "opened_at")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.opened_at < self.created_at:
            raise ValueError("opened_at cannot precede created_at")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if self.task_lease_id is not None and not isinstance(self.task_lease_id, UUID):
            raise TypeError("task_lease_id must be a UUID or None")
        if self.task_lease_fence is not None and (
            isinstance(self.task_lease_fence, bool) or not isinstance(self.task_lease_fence, int)
        ):
            raise TypeError("task_lease_fence must be an integer or None")
        _optional_pair(
            self.task_lease_id, self.task_lease_fence, "task_lease_id", "task_lease_fence"
        )
        if self.task_lease_fence is not None and self.task_lease_fence < 1:
            raise AgentSessionStateError("a recorded task lease requires task_lease_fence >= 1")
        _optional_pair(
            self.checkpoint_ref, self.checkpoint_sha256, "checkpoint_ref", "checkpoint_sha256"
        )
        if self.checkpoint_sha256 is not None:
            _sha256_hex(self.checkpoint_sha256, "checkpoint_sha256")
        if self.reconciliation is not None:
            object.__setattr__(
                self, "reconciliation", _mapping(self.reconciliation, "reconciliation")
            )
        _optional_pair(
            self.reconciliation,
            self.reconciliation_sha256,
            "reconciliation",
            "reconciliation_sha256",
        )
        if self.reconciliation_sha256 is not None:
            _sha256_hex(self.reconciliation_sha256, "reconciliation_sha256")
        if self.last_activity_at is not None:
            _aware(self.last_activity_at, "last_activity_at")
        if (self.state in CLOSED_AT_REQUIRED_SESSION_STATES) != (self.closed_at is not None):
            raise AgentSessionStateError("closed_at is set exactly in closed/failed")
        if self.closed_at is not None:
            _aware(self.closed_at, "closed_at")
            if self.closed_at < self.opened_at:
                raise ValueError("closed_at cannot precede opened_at")
        if self.close_reason is not None:
            if not isinstance(self.close_reason, str) or not self.close_reason.strip():
                raise ValueError("close_reason must be a non-empty string")
            if self.state not in (
                AgentSessionState.CLOSED,
                AgentSessionState.FAILED,
                AgentSessionState.LOST,
            ):
                raise AgentSessionStateError(
                    "close_reason is only meaningful for closed/failed/lost"
                )

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_AGENT_SESSION_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_AGENT_SESSION_STATES

    def capabilities(self) -> OperatorCapabilitiesV1:
        return OperatorCapabilitiesV1.model_validate(self.capability_probe)

    def reconciliation_outcome(self) -> SessionReconcileOutcome | None:
        if self.reconciliation is None:
            return None
        return SessionReconcileOutcome(self.reconciliation["outcome"])

    def with_state(
        self,
        state: AgentSessionState,
        *,
        now: datetime,
        close_reason: str | None = None,
    ) -> AgentSessionV1:
        """One CAS state change; the caller persists it against ``state_version``."""
        validate_session_transition(self.state, state)
        timestamp = _aware(now, "now")
        return replace(
            self,
            state=state,
            state_version=self.state_version + 1,
            close_reason=close_reason,
            closed_at=timestamp if state in CLOSED_AT_REQUIRED_SESSION_STATES else None,
            last_activity_at=timestamp,
            updated_at=timestamp,
        )

    def with_task_lease(self, lease_id: UUID, fence: int, *, now: datetime) -> AgentSessionV1:
        if self.state is not AgentSessionState.RUNNING:
            raise AgentSessionStateError("only a running session records a task lease")
        if not isinstance(lease_id, UUID):
            raise TypeError("lease_id must be a UUID")
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise ValueError("fence must be an integer >= 1")
        return replace(
            self,
            task_lease_id=lease_id,
            task_lease_fence=fence,
            state_version=self.state_version + 1,
            last_activity_at=_aware(now, "now"),
            updated_at=now,
        )

    def with_host_session_ref(self, host_session_ref: str, *, now: datetime) -> AgentSessionV1:
        if not isinstance(host_session_ref, str) or not host_session_ref.strip():
            raise ValueError("host_session_ref must not be empty")
        return replace(
            self,
            host_session_ref=host_session_ref.strip(),
            state_version=self.state_version + 1,
            updated_at=_aware(now, "now"),
        )

    def with_checkpoint(
        self, checkpoint_ref: str, checkpoint_sha256: str, *, now: datetime
    ) -> AgentSessionV1:
        if not isinstance(checkpoint_ref, str) or not checkpoint_ref.strip():
            raise ValueError("checkpoint_ref must not be empty")
        _sha256_hex(checkpoint_sha256, "checkpoint_sha256")
        return replace(
            self,
            checkpoint_ref=checkpoint_ref.strip(),
            checkpoint_sha256=checkpoint_sha256,
            state_version=self.state_version + 1,
            last_activity_at=_aware(now, "now"),
            updated_at=now,
        )

    def with_reconciliation(
        self,
        reconciliation: Mapping[str, Any],
        reconciliation_sha256: str,
        *,
        now: datetime,
    ) -> AgentSessionV1:
        _sha256_hex(reconciliation_sha256, "reconciliation_sha256")
        return replace(
            self,
            reconciliation=_mapping(reconciliation, "reconciliation"),
            reconciliation_sha256=reconciliation_sha256,
            state_version=self.state_version + 1,
            last_activity_at=_aware(now, "now"),
            updated_at=now,
        )

    def with_last_activity(self, *, now: datetime) -> AgentSessionV1:
        return replace(
            self,
            state_version=self.state_version + 1,
            last_activity_at=_aware(now, "now"),
            updated_at=now,
        )

    def to_payload(self) -> dict[str, Any]:
        """Public, log-safe representation; never includes auth material."""
        return {
            "id": str(self.id),
            "investigation_id": str(self.investigation_id),
            "task_id": str(self.task_id),
            "adapter_kind": self.adapter_kind.value,
            "state": self.state.value,
            "state_version": self.state_version,
            "host_session_ref": self.host_session_ref,
            "handoff_id": str(self.handoff_id) if self.handoff_id is not None else None,
            "capability_sha256": self.capability_sha256,
            "probed_at": self.probed_at.isoformat(),
            "task_lease_id": str(self.task_lease_id) if self.task_lease_id is not None else None,
            "task_lease_fence": self.task_lease_fence,
            "checkpoint_ref": self.checkpoint_ref,
            "checkpoint_sha256": self.checkpoint_sha256,
            "reconciliation": (
                thaw_json(self.reconciliation) if self.reconciliation is not None else None
            ),
            "reconciliation_sha256": self.reconciliation_sha256,
            "last_activity_at": (
                self.last_activity_at.isoformat() if self.last_activity_at is not None else None
            ),
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at is not None else None,
            "close_reason": self.close_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


_DELIVERY_EVIDENCE_STATES = frozenset({InstructionState.DELIVERED, InstructionState.ACKNOWLEDGED})
_IN_FLIGHT_DELIVERY_STATES = frozenset(
    {InstructionState.DELIVERING, InstructionState.DELIVERY_UNKNOWN}
)
_EVIDENCE_FREE_STATES = frozenset({InstructionState.QUEUED, InstructionState.CANCELLED})


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSessionInstructionV1:
    """One durably queued human instruction for one session (D05, F3-D §3.2).

    The delivery columns mirror migration 0027's CHECKs: an attempt records
    its token, number and exact text SHA-256 before any host effect, a proven
    delivery records its mode and note diffs, and ``delivery_unknown`` keeps
    the token so an ambiguous effect is reconciled and never blind-resubmitted.
    Evidence-free ``delivered``/``acknowledged`` rows stay valid because rows
    queued before 0027 never carried that evidence; the upgrade never invents
    a token, mode, digest or diff for them.
    """

    investigation_id: UUID
    task_id: UUID
    session_id: UUID
    idempotency_key: str
    text: str = field(repr=False)
    text_sha256: str
    state: InstructionState
    created_by_kind: str
    created_by_id: str
    queued_at: datetime
    created_at: datetime
    updated_at: datetime
    id: UUID = field(default_factory=uuid4)
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    delivery_attempts: int = 0
    delivery_token: UUID | None = None
    delivery_mode: InstructionDeliveryMode | None = None
    delivery_sha256: str | None = None
    delivery_note_diffs: tuple[Mapping[str, Any], ...] | None = None
    delivery_attempted_at: datetime | None = None
    delivery_unknown_at: datetime | None = None
    last_delivery_error: str | None = None
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID):
            raise TypeError("id must be a UUID")
        for name in ("investigation_id", "task_id", "session_id"):
            if not isinstance(getattr(self, name), UUID):
                raise TypeError(f"{name} must be a UUID")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        normalized_text = self.text.strip()
        if not normalized_text:
            raise ValueError("instruction text must not be empty")
        if len(normalized_text.encode("utf-8")) > INSTRUCTION_MAX_TEXT_BYTES:
            raise ValueError("instruction text is bounded to 64 KiB")
        object.__setattr__(self, "text", normalized_text)
        _sha256_hex(self.text_sha256, "text_sha256")
        if self.text_sha256 != hashlib.sha256(normalized_text.encode("utf-8")).hexdigest():
            raise AgentSessionStateError("instruction text hash disagrees with its body")
        if not isinstance(self.state, InstructionState):
            raise TypeError("state must use InstructionState")
        if self.created_by_kind not in ("human", "operator"):
            raise AgentSessionStateError("created_by_kind must be human or operator")
        if not isinstance(self.created_by_id, str) or not self.created_by_id.strip():
            raise ValueError("created_by_id must not be empty")
        _aware(self.queued_at, "queued_at")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        for name in (
            "delivered_at",
            "acknowledged_at",
            "delivery_attempted_at",
            "delivery_unknown_at",
            "cancelled_at",
        ):
            value = getattr(self, name)
            if value is not None:
                _aware(value, name)
        if isinstance(self.delivery_attempts, bool) or not isinstance(self.delivery_attempts, int):
            raise TypeError("delivery_attempts must be an integer")
        if self.delivery_attempts < 0:
            raise AgentSessionStateError("delivery_attempts must be >= 0")
        proven = self.state in _DELIVERY_EVIDENCE_STATES
        if proven != (self.delivered_at is not None):
            raise AgentSessionStateError(
                "delivered_at is set exactly when delivered or acknowledged"
            )
        if (self.state is InstructionState.ACKNOWLEDGED) != (self.acknowledged_at is not None):
            raise AgentSessionStateError("acknowledged_at is set exactly when acknowledged")
        if (self.state is InstructionState.DELIVERY_UNKNOWN) != (
            self.delivery_unknown_at is not None
        ):
            raise AgentSessionStateError("delivery_unknown_at is set exactly when delivery_unknown")
        if (self.state is InstructionState.CANCELLED) != (self.cancelled_at is not None):
            raise AgentSessionStateError("cancelled_at is set exactly when cancelled")
        if (self.state is InstructionState.CANCELLED) != (self.cancel_reason is not None):
            raise AgentSessionStateError("cancel_reason is set exactly when cancelled")
        if self.cancel_reason is not None:
            if not isinstance(self.cancel_reason, str) or not self.cancel_reason.strip():
                raise ValueError("cancel_reason must be a non-empty string")
            if len(self.cancel_reason) > 4096:
                raise ValueError("cancel_reason is bounded to 4096 characters")
        if self.delivery_token is not None and not isinstance(self.delivery_token, UUID):
            raise TypeError("delivery_token must be a UUID or None")
        if self.delivery_token is None and self.state in _IN_FLIGHT_DELIVERY_STATES:
            raise AgentSessionStateError(
                "a delivering/delivery_unknown instruction records its delivery_token"
            )
        if self.delivery_token is not None and self.state in _EVIDENCE_FREE_STATES:
            raise AgentSessionStateError("a queued/cancelled instruction carries no delivery_token")
        if self.delivery_mode is not None:
            if not isinstance(self.delivery_mode, InstructionDeliveryMode):
                raise TypeError("delivery_mode must use InstructionDeliveryMode")
            if not proven:
                raise AgentSessionStateError("delivery_mode is recorded only at delivery")
        if self.delivery_sha256 is not None:
            _sha256_hex(self.delivery_sha256, "delivery_sha256")
            if self.state in _EVIDENCE_FREE_STATES:
                raise AgentSessionStateError(
                    "a queued/cancelled instruction carries no delivery_sha256"
                )
        if self.state in _IN_FLIGHT_DELIVERY_STATES and self.delivery_sha256 is None:
            raise AgentSessionStateError(
                "a delivering/delivery_unknown instruction records delivery_sha256"
            )
        if self.delivery_note_diffs is not None:
            note_diffs = freeze_json(self.delivery_note_diffs)
            if not isinstance(note_diffs, tuple) or not all(
                isinstance(item, Mapping) for item in note_diffs
            ):
                raise ValueError("delivery_note_diffs must be a JSON array of objects")
            if not proven:
                raise AgentSessionStateError("delivery_note_diffs is recorded only at delivery")
            object.__setattr__(self, "delivery_note_diffs", note_diffs)
        if self.last_delivery_error is not None:
            if (
                not isinstance(self.last_delivery_error, str)
                or not self.last_delivery_error.strip()
            ):
                raise ValueError("last_delivery_error must be a non-empty string")
            if proven:
                raise AgentSessionStateError("a delivered instruction has no last_delivery_error")

    @property
    def text_bytes(self) -> bytes:
        return self.text.encode("utf-8")

    def with_state(
        self,
        state: InstructionState,
        *,
        now: datetime,
        delivery_token: UUID | None = None,
        delivery_sha256: str | None = None,
        delivery_mode: InstructionDeliveryMode | None = None,
        delivery_note_diffs: Sequence[Mapping[str, Any]] | None = None,
        last_delivery_error: str | None = None,
        cancel_reason: str | None = None,
    ) -> AgentSessionInstructionV1:
        """One validated transition of the F3-D §3.2 instruction machine.

        Transitions that need evidence take it as keyword arguments: a
        reservation requires the fresh attempt token and the exact delivered
        text SHA-256, and a cancellation requires its bounded human reason.
        """
        validate_instruction_transition(self.state, state)
        timestamp = _aware(now, "now")
        token = self.delivery_token
        delivery_sha = self.delivery_sha256
        mode = self.delivery_mode
        note_diffs = self.delivery_note_diffs
        attempts = self.delivery_attempts
        attempted_at = self.delivery_attempted_at
        unknown_at = self.delivery_unknown_at
        delivered_at = self.delivered_at
        acknowledged_at = self.acknowledged_at
        cancelled_at = self.cancelled_at
        reason = self.cancel_reason
        error = self.last_delivery_error
        if state is InstructionState.DELIVERING:
            token = delivery_token
            delivery_sha = delivery_sha256
            mode = None
            note_diffs = None
            attempts += 1
            attempted_at = timestamp
            unknown_at = None
            error = None
        elif state is InstructionState.DELIVERED:
            delivered_at = timestamp
            mode = delivery_mode if delivery_mode is not None else self.delivery_mode
            delivery_sha = delivery_sha256 if delivery_sha256 is not None else self.delivery_sha256
            note_diffs = (
                tuple(delivery_note_diffs)
                if delivery_note_diffs is not None
                else self.delivery_note_diffs
            )
            unknown_at = None
            error = None
        elif state is InstructionState.ACKNOWLEDGED:
            acknowledged_at = timestamp
            unknown_at = None
            error = None
        elif state is InstructionState.DELIVERY_UNKNOWN:
            unknown_at = timestamp
            error = (
                last_delivery_error if last_delivery_error is not None else self.last_delivery_error
            )
        elif state is InstructionState.QUEUED:
            token = None
            delivery_sha = None
            mode = None
            note_diffs = None
            unknown_at = None
            error = last_delivery_error
        elif state is InstructionState.CANCELLED:
            token = None
            delivery_sha = None
            mode = None
            note_diffs = None
            unknown_at = None
            cancelled_at = timestamp
            reason = cancel_reason
        return replace(
            self,
            state=state,
            delivered_at=delivered_at,
            acknowledged_at=acknowledged_at,
            delivery_attempts=attempts,
            delivery_token=token,
            delivery_mode=mode,
            delivery_sha256=delivery_sha,
            delivery_note_diffs=note_diffs,
            delivery_attempted_at=attempted_at,
            delivery_unknown_at=unknown_at,
            last_delivery_error=error,
            cancelled_at=cancelled_at,
            cancel_reason=reason,
            updated_at=timestamp,
        )

    def with_delivery_reserved(
        self, *, token: UUID, delivery_sha256: str, now: datetime
    ) -> AgentSessionInstructionV1:
        """Reserve one attempt (queued -> delivering).

        An ambiguous ``delivery_unknown`` row is never re-reserved directly:
        §3.2/§3.5 require proving absence first, which returns it to ``queued``.
        """
        if self.state is not InstructionState.QUEUED:
            raise AgentSessionStateError("only a queued instruction is reserved")
        return self.with_state(
            InstructionState.DELIVERING,
            now=now,
            delivery_token=token,
            delivery_sha256=delivery_sha256,
        )

    def with_delivery_confirmed(
        self,
        *,
        delivery_mode: InstructionDeliveryMode,
        delivery_sha256: str,
        delivery_note_diffs: Sequence[Mapping[str, Any]],
        now: datetime,
    ) -> AgentSessionInstructionV1:
        """Record a proven receipt (delivering|delivery_unknown -> delivered)."""
        if self.state not in (InstructionState.DELIVERING, InstructionState.DELIVERY_UNKNOWN):
            raise AgentSessionStateError("only an in-flight instruction is delivered")
        return self.with_state(
            InstructionState.DELIVERED,
            now=now,
            delivery_mode=delivery_mode,
            delivery_sha256=delivery_sha256,
            delivery_note_diffs=delivery_note_diffs,
        )

    def with_cancellation(self, *, reason: str, now: datetime) -> AgentSessionInstructionV1:
        """Cancel a never-delivered instruction (queued|delivery_unknown -> cancelled)."""
        if self.state not in (InstructionState.QUEUED, InstructionState.DELIVERY_UNKNOWN):
            raise AgentSessionStateError("only a never-delivered instruction is cancelled")
        return self.with_state(InstructionState.CANCELLED, now=now, cancel_reason=reason)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "investigation_id": str(self.investigation_id),
            "task_id": str(self.task_id),
            "session_id": str(self.session_id),
            "state": self.state.value,
            "text_sha256": self.text_sha256,
            "queued_at": self.queued_at.isoformat(),
            "delivered_at": (
                self.delivered_at.isoformat() if self.delivered_at is not None else None
            ),
            "acknowledged_at": (
                self.acknowledged_at.isoformat() if self.acknowledged_at is not None else None
            ),
            "delivery_attempts": self.delivery_attempts,
            "delivery_token": (
                str(self.delivery_token) if self.delivery_token is not None else None
            ),
            "delivery_mode": self.delivery_mode.value if self.delivery_mode is not None else None,
            "delivery_sha256": self.delivery_sha256,
            "delivery_note_diffs": (
                [thaw_json(item) for item in self.delivery_note_diffs]
                if self.delivery_note_diffs is not None
                else None
            ),
            "delivery_attempted_at": (
                self.delivery_attempted_at.isoformat()
                if self.delivery_attempted_at is not None
                else None
            ),
            "delivery_unknown_at": (
                self.delivery_unknown_at.isoformat()
                if self.delivery_unknown_at is not None
                else None
            ),
            "last_delivery_error": self.last_delivery_error,
            "cancelled_at": (
                self.cancelled_at.isoformat() if self.cancelled_at is not None else None
            ),
            "cancel_reason": self.cancel_reason,
            "created_by": {"kind": self.created_by_kind, "actor_id": self.created_by_id},
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def instruction_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "ACTIVE_AGENT_SESSION_STATES",
    "AGENT_SESSION_SCHEMA_VERSION",
    "AGENT_SESSION_SECRET_NONCE_BYTES",
    "CLOSED_AT_REQUIRED_SESSION_STATES",
    "INSTRUCTION_MAX_TEXT_BYTES",
    "INSTRUCTION_PENDING_STATES",
    "INSTRUCTION_SCHEMA_VERSION",
    "INSTRUCTION_STATE_TRANSITIONS",
    "OPERATOR_CAPABILITIES_SCHEMA_VERSION",
    "RECONCILE_OUTCOMES_RESUMABLE",
    "RECONCILE_OUTCOMES_WITH_HANDOFF",
    "RECONCILE_OUTCOME_STATE",
    "RESUMABLE_AGENT_SESSION_STATES",
    "SESSION_STATE_TRANSITIONS",
    "TERMINAL_AGENT_SESSION_STATES",
    "TERMINAL_INSTRUCTION_STATES",
    "AgentSessionInstructionV1",
    "AgentSessionState",
    "AgentSessionStateError",
    "AgentSessionV1",
    "AgenticOperatorError",
    "AgenticOperatorPort",
    "CollectResultsRequest",
    "HandoffRequestReceiptV1",
    "InstructionDeliveryMode",
    "InstructionDeliveryOutcome",
    "InstructionReceiptV1",
    "InstructionState",
    "InterruptSessionRequest",
    "InterruptSessionResultV1",
    "OperatorAdapterKind",
    "OperatorCapabilitiesV1",
    "OperatorProbeCommand",
    "OperatorResultItemV1",
    "OperatorResultsV1",
    "PrepareSessionRequest",
    "PreparedOperatorSessionV1",
    "ProbeCapabilitiesRequest",
    "ReconcileSessionRequest",
    "RequestHandoffRequest",
    "ResumeSessionRequest",
    "ResumeSessionResultV1",
    "SendInstructionRequest",
    "SessionProcessStatusV1",
    "SessionReconcileOutcome",
    "SessionReconciliationV1",
    "SessionStatusRequest",
    "SessionTransitionError",
    "StartSessionRequest",
    "instruction_text_sha256",
    "validate_instruction_transition",
    "validate_session_transition",
]

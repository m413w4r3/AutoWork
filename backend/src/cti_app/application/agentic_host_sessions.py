"""Typed wire contract for the host session supervisor (SESSION-RUNNER.md §6).

The backend talks to ``autowork-workspace-agent`` over the same AF_UNIX socket,
bearer token and error envelope as the workspace routes.  This module is the
application-side half of lot M24-B: every ``/v1/sessions/**`` payload the
backend can send or accept is mirrored here as a frozen strict Pydantic model,
and :class:`HostSessionsPort` is the narrow seam the Codex/Claude adapters and
the one-time secret delivery implement against.

The supervisor is a technical mirror of the session, never an authority: these
models carry host facts only (state, cursors, exit records, captured relative
paths).  A response may never carry an absolute host path — the runtime
directory, the workspace root and the session scratch stay on the host — so
every response model rejects one wherever it appears.  Session authentication
material is not part of the wire contract except for the one-time
``auth_secret`` handed over at prepare; that field is excluded from ``repr``.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from cti_app.application.workspace_agent import WorkspaceAgentError
from cti_app.domain.agentic_operator import OperatorAdapterKind

HOST_SESSIONS_PROTOCOL: Literal["workspace-agent.sessions.v1"] = "workspace-agent.sessions.v1"
# The workspace agent bounds JSON bodies to 64 KiB and the tool manifest to
# 512 KiB; generic responses are bounded to 16 MiB (WORKSPACE-AGENT §4).
HOST_JSON_BODY_LIMIT = 64 * 1024
HOST_TOOL_MANIFEST_BODY_LIMIT = 512 * 1024
HOST_RESPONSE_LIMIT = 16 * 1024 * 1024
HOST_MAX_PAGE_SIZE = 500
HOST_MAX_INSTRUCTION_BYTES = 32 * 1024
HOST_MAX_RUN_PROMPT_BYTES = 32 * 1024
HOST_MAX_REASON_CHARS = 256
HOST_MAX_MANIFEST_TOOLS = 512
HOST_MAX_SKILLS = 32
HOST_MAX_ALLOWED_PATHS = 64
HOST_MAX_TEXT_CHARS = 8192
HOST_SECRET_BYTES = 32

_ADAPTER_KIND_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
_WORKSPACE_ID_PATTERN = r"^ws-[0-9a-f]{32}$"
_SKILL_NAME_PATTERN = r"^autowork-[a-z0-9][a-z0-9-]{0,63}$"
_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"

# An absolute host path anywhere in a supervisor response is a contract
# violation: the backend never learns where the runtime or workspace lives.
_ABSOLUTE_HOST_PATH = re.compile(
    r"(?:^|[^\w])/(?:home|root|tmp|var|usr|etc|opt|mnt|srv|run|proc|sys|dev)/"
    r"|(?:^|[^\w])~[\\/]"
    r"|[A-Za-z]:[\\/]"
)


class HostSessionsError(WorkspaceAgentError):
    """A typed refusal from the session supervisor or the wire client."""

    code = "host_sessions_error"
    http_status = 502


class HostInvalidRequestError(HostSessionsError):
    code = "invalid_request"
    http_status = 400


class HostSessionNotFoundError(HostSessionsError):
    code = "session_not_found"
    http_status = 404


class HostSessionStateConflictError(HostSessionsError):
    code = "session_state_conflict"
    http_status = 409


class HostSessionBusyError(HostSessionsError):
    code = "session_busy"
    http_status = 409


class HostConfinementUnavailableError(HostSessionsError):
    code = "confinement_unavailable"
    http_status = 409


class HostCapabilityUnavailableError(HostSessionsError):
    code = "capability_unavailable"
    http_status = 409


class HostCapabilityUnsupportedError(HostSessionsError):
    """The recorded probe does not establish the capability a call needs.

    Raised before any wire call; the adapter never guesses a CLI mode.
    """

    code = "capability_unavailable"
    http_status = 409


class HostResumeUnavailableError(HostSessionsError):
    code = "resume_unavailable"
    http_status = 409


class HostIdempotencyConflictError(HostSessionsError):
    code = "idempotency_conflict"
    http_status = 409


class HostBudgetExceededError(HostSessionsError):
    code = "budget_exceeded"
    http_status = 409


class HostManifestMissingError(HostSessionsError):
    code = "manifest_missing"
    http_status = 409


class HostSecretUnavailableError(HostSessionsError):
    code = "secret_unavailable"
    http_status = 503


class HostSecretHandoffConsumedError(HostSessionsError):
    """A session already received its one-time secret hand-off."""

    code = "secret_already_delivered"
    http_status = 409


class HostSessionContractError(HostSessionsError):
    """The supervisor (or an adapter) answered outside the typed contract."""

    code = "invalid_response"
    http_status = 502


_HOST_WIRE_ERRORS: Mapping[str, type[HostSessionsError]] = {
    "invalid_request": HostInvalidRequestError,
    "session_not_found": HostSessionNotFoundError,
    "session_state_conflict": HostSessionStateConflictError,
    "session_busy": HostSessionBusyError,
    "confinement_unavailable": HostConfinementUnavailableError,
    "capability_unavailable": HostCapabilityUnavailableError,
    "resume_unavailable": HostResumeUnavailableError,
    "idempotency_conflict": HostIdempotencyConflictError,
    "budget_exceeded": HostBudgetExceededError,
    "manifest_missing": HostManifestMissingError,
    "secret_unavailable": HostSecretUnavailableError,
}


def host_error_from_code(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: Mapping[str, JsonValue] | None = None,
) -> HostSessionsError:
    error_class = _HOST_WIRE_ERRORS.get(code)
    if error_class is None:
        error = HostSessionsError(message)
        error.code = code
        error.retryable = retryable
        error.details = dict(details or {})
        return error
    return error_class(message, retryable=retryable, details=details)


class _HostWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _HostResponseModel(_HostWireModel):
    @model_validator(mode="after")
    def _reject_absolute_host_paths(self) -> _HostResponseModel:
        _scan_for_absolute_host_paths(self.model_dump(mode="python"))
        return self


def _scan_for_absolute_host_paths(value: object) -> None:
    if isinstance(value, str):
        if _ABSOLUTE_HOST_PATH.search(value):
            raise ValueError("a supervisor response must not carry an absolute host path")
    elif isinstance(value, Mapping):
        for item in value.values():
            _scan_for_absolute_host_paths(item)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for item in value:
            _scan_for_absolute_host_paths(item)


def _sha256(value: str, name: str) -> str:
    if re.fullmatch(_SHA256_PATTERN, value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _relative_workspace_path(value: str, name: str) -> str:
    if not value or len(value) > 1024 or "\x00" in value:
        raise ValueError(f"{name} must be a bounded non-empty relative path")
    if value.startswith("/") or "\\" in value:
        raise ValueError(f"{name} must be relative to the workspace root")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{name} must not contain empty, '.' or '..' segments")
    return value


class HostCapabilityFlagsV1(_HostResponseModel):
    non_interactive: bool
    json_events: bool
    structured_output: bool
    resume: bool
    streaming_stdin: bool
    mcp_per_invocation: bool
    os_sandbox: bool
    # F3-D §3.4: the host can start a fresh next run carrying a delivered
    # instruction's text.  The field is additive (backend first): an older
    # host that never announces it keeps validating, and the flag stays false
    # so no instruction is ever delivered on an unproven capability.
    next_run_instruction: bool = False

    def proven_modes(self) -> tuple[str, ...]:
        """The probed flag names that are true, in contract order."""
        pairs = (
            ("non_interactive", self.non_interactive),
            ("json_events", self.json_events),
            ("structured_output", self.structured_output),
            ("resume", self.resume),
            ("streaming_stdin", self.streaming_stdin),
            ("mcp_per_invocation", self.mcp_per_invocation),
            ("os_sandbox", self.os_sandbox),
        )
        return tuple(name for name, proven in pairs if proven)


class HostConfinementV1(_HostResponseModel):
    profile: str = Field(min_length=1, max_length=64)
    bwrap_ok: bool
    netns: bool = False
    # R03 B1: what the host actually attested for the reasoning-session network.
    network_mode: Literal["isolated_egress"] | None = None
    loopback_isolated: bool = False
    reason: str | None = Field(default=None, max_length=512)
    bwrap_reason: str | None = Field(default=None, max_length=512)
    network_reason: str | None = Field(default=None, max_length=512)
    mocked: bool = False
    bwrap_sha256: str = ""
    capabilities: Mapping[str, JsonValue] = Field(default_factory=dict)


class HostCapabilityProbeV1(_HostResponseModel):
    """``GET /v1/sessions/capabilities`` — real host facts, never invented."""

    protocol: Literal["workspace-agent.sessions.v1"] = HOST_SESSIONS_PROTOCOL
    adapter_kind: str = Field(pattern=_ADAPTER_KIND_PATTERN)
    version: str = Field(min_length=1, max_length=256)
    binary_sha256: str
    help_sha256: str
    flags: HostCapabilityFlagsV1
    confinement: HostConfinementV1
    probed_at: datetime
    probe_sha256: str

    @field_validator("binary_sha256", "help_sha256", "probe_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "capability digest")

    @field_validator("probed_at")
    @classmethod
    def _validate_probed_at(cls, value: datetime) -> datetime:
        return _aware(value, "probed_at")


class HostHandoffRefV1(_HostWireModel):
    handoff_id: UUID
    version: int = Field(ge=1)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "handoff sha256")


class HostBudgetV1(_HostWireModel):
    wall_seconds: int = Field(ge=1, le=24 * 60 * 60)
    max_output_bytes: int = Field(ge=1, le=256 * 1024 * 1024)
    max_instructions: int = Field(ge=1, le=10_000)


class HostPrepareSessionRequestV1(_HostWireModel):
    """``POST /v1/sessions`` — the one request that carries the session secret."""

    session_id: UUID
    adapter_kind: str = Field(pattern=_ADAPTER_KIND_PATTERN)
    workspace_id: str = Field(pattern=_WORKSPACE_ID_PATTERN)
    investigation_id: UUID
    task_id: UUID
    handoff: HostHandoffRefV1
    lease_fence: int = Field(ge=0)
    allowed_workspace_paths: tuple[str, ...] = Field(max_length=HOST_MAX_ALLOWED_PATHS)
    skill_names: tuple[str, ...] = Field(max_length=HOST_MAX_SKILLS)
    budgets: HostBudgetV1
    model: str | None = Field(default=None, pattern=_MODEL_PATTERN)
    # R03 B1: the backend's configured CLI network mode; the host must meet it.
    network_mode: Literal["isolated_egress"] = "isolated_egress"
    auth_secret: str = Field(max_length=128, repr=False)

    @field_validator("allowed_workspace_paths")
    @classmethod
    def _validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_workspace_path(item, "allowed_workspace_paths") for item in value)

    @field_validator("skill_names")
    @classmethod
    def _validate_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if re.fullmatch(_SKILL_NAME_PATTERN, item) is None:
                raise ValueError("skill_names entries must name an autowork-* skill")
        return value

    @field_validator("auth_secret")
    @classmethod
    def _validate_secret(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise ValueError("auth_secret must be base64") from exc
        if len(decoded) != HOST_SECRET_BYTES:
            raise ValueError("auth_secret must decode to exactly 32 bytes")
        return value


class HostPreparedSessionV1(_HostResponseModel):
    session_id: UUID
    state: Literal["ready"]
    state_version: int = Field(ge=1)
    capability_sha256: str
    confinement: HostConfinementV1
    prompt_sha256: str

    @field_validator("capability_sha256", "prompt_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "prepare digest")


class HostStartSessionRequestV1(_HostWireModel):
    run_prompt: str | None = Field(default=None, max_length=HOST_MAX_RUN_PROMPT_BYTES)
    instruction_id: UUID | None = None

    @field_validator("run_prompt")
    @classmethod
    def _validate_prompt(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > HOST_MAX_RUN_PROMPT_BYTES:
            raise ValueError("run_prompt exceeds the host session prompt bound")
        return value


class HostStartedSessionV1(_HostResponseModel):
    session_id: UUID
    state: Literal["running"]
    state_version: int = Field(ge=1)
    run: int = Field(ge=1)
    log_cursor: int = Field(ge=0)


class HostExitV1(_HostResponseModel):
    code: int | None = Field(default=None, ge=0)
    signal: int | None = Field(default=None, ge=0)


class HostRunExitV1(_HostResponseModel):
    """The per-run exit record; the status/collect ``exit`` is the shorter pair."""

    run: int = Field(ge=1)
    code: int | None = Field(default=None, ge=0)
    signal: int | None = Field(default=None, ge=1, le=64)
    at: datetime


class HostRunV1(_HostResponseModel):
    run: int = Field(ge=1)
    state: Literal["running", "exited"]
    pid: int | None = Field(default=None, ge=1)
    pgid: int | None = Field(default=None, ge=2)
    start_ticks: int | None = Field(default=None, ge=0)
    started_at: datetime
    log_start: int = Field(ge=0)
    ended_at: datetime | None = None
    exit: HostRunExitV1 | None = None


class HostBudgetUsageV1(_HostResponseModel):
    wall_seconds: float = Field(ge=0)
    output_bytes: int = Field(ge=0)
    instructions: int = Field(ge=0)


class HostSessionStatusV1(_HostResponseModel):
    state: Literal[
        "provisioning",
        "ready",
        "running",
        "awaiting_instruction",
        "interrupt_requested",
        "stopped",
        "lost",
        "reconciling",
        "closed",
        "failed",
    ]
    state_version: int = Field(ge=1)
    adapter_kind: str = Field(pattern=_ADAPTER_KIND_PATTERN)
    runs: tuple[HostRunV1, ...] = ()
    budgets_used: HostBudgetUsageV1
    last_activity_at: datetime
    exit: HostExitV1 | None = None
    log_cursor: int = Field(ge=0)
    checkpoint_sha256: str | None = None

    @field_validator("checkpoint_sha256")
    @classmethod
    def _validate_checkpoint(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, "checkpoint_sha256")


class HostSessionEventV1(_HostResponseModel):
    seq: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=64)
    at: datetime
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class HostSessionEventsPageV1(_HostResponseModel):
    generation: int = Field(ge=0)
    events: tuple[HostSessionEventV1, ...] = ()
    next_cursor: int = Field(ge=0)
    oldest_retained: int = Field(ge=0)
    resync_required: bool


class HostLogLineV1(_HostResponseModel):
    seq: int = Field(ge=0)
    stream: Literal["stdout", "stderr"]
    text: str = Field(max_length=HOST_MAX_TEXT_CHARS)
    truncated: bool


class HostLogsPageV1(_HostResponseModel):
    lines: tuple[HostLogLineV1, ...] = ()
    next_cursor: int = Field(ge=0)


class HostInstructionRequestV1(_HostWireModel):
    instruction_id: UUID
    text: str = Field(min_length=1, max_length=HOST_MAX_INSTRUCTION_BYTES)
    sha256: str

    @field_validator("text")
    @classmethod
    def _validate_text_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > HOST_MAX_INSTRUCTION_BYTES:
            raise ValueError("instruction text exceeds the host session bound")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "instruction sha256")


class HostInstructionResultV1(_HostResponseModel):
    accepted: bool
    delivered: Literal["live", "queued"]
    cursor: int = Field(ge=0)


class HostReasonRequestV1(_HostWireModel):
    reason: str = Field(min_length=1, max_length=HOST_MAX_REASON_CHARS)


class HostInterruptResultV1(_HostResponseModel):
    state: Literal["interrupt_requested"]
    signal_sent: bool


class HostResumeRequestV1(_HostWireModel):
    reason: str = Field(min_length=1, max_length=HOST_MAX_REASON_CHARS)
    instruction_id: UUID | None = None


class HostReconcileExitV1(_HostWireModel):
    code: int | None = Field(default=None, ge=0)
    signal: int | None = Field(default=None, ge=0)


class HostReconcileRequestV1(_HostWireModel):
    observed_exit: HostReconcileExitV1 | None = None


class HostReconcileEvidenceV1(_HostResponseModel):
    last_event_seq: int = Field(ge=0)
    exit: HostExitV1 | None = None
    process_start_ticks: int | None = Field(default=None, ge=0)


class HostReconcileResultV1(_HostResponseModel):
    outcome: Literal[
        "session_alive",
        "terminated_clean",
        "lost_with_handoff",
        "lost_unreconciled",
        "resume_available",
        "resume_unavailable",
    ]
    evidence: HostReconcileEvidenceV1


class HostCollectRequestV1(_HostWireModel):
    capture: bool


class HostCapturedFileV1(_HostResponseModel):
    path: str = Field(min_length=1, max_length=1024)
    sha256: str

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _relative_workspace_path(value, "captured file path")

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "captured file sha256")


class HostLogRangeV1(_HostResponseModel):
    cursor_from: int = Field(ge=0)
    cursor_to: int = Field(ge=0)


class HostSessionResultV1(_HostResponseModel):
    outcome: str | None = Field(default=None, max_length=64)
    exit: HostExitV1 | None = None
    final_message_sha256: str | None = None
    files: tuple[HostCapturedFileV1, ...] = ()
    log_range: HostLogRangeV1
    captured_notes: tuple[str, ...] = ()
    collected_at: datetime

    @field_validator("final_message_sha256")
    @classmethod
    def _validate_final_sha256(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, "final_message_sha256")

    @field_validator("captured_notes")
    @classmethod
    def _validate_notes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_workspace_path(item, "captured note") for item in value)


class HostCloseResultV1(_HostResponseModel):
    state: Literal["closed"]


class HostToolManifestEntryV1(_HostWireModel):
    mcp_name: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    broker_name: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    schema_version: str = Field(min_length=1, max_length=32)
    input_schema: dict[str, JsonValue]
    effect: str = Field(min_length=1, max_length=32)
    cost: str = Field(min_length=1, max_length=64)
    capability: str | None = Field(default=None, max_length=64)


class HostToolManifestRequestV1(_HostWireModel):
    manifest_sha256: str
    tools: tuple[HostToolManifestEntryV1, ...] = Field(max_length=HOST_MAX_MANIFEST_TOOLS)

    @field_validator("manifest_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "manifest_sha256")


class HostToolManifestAcceptedV1(_HostResponseModel):
    accepted: bool
    manifest_sha256: str

    @field_validator("manifest_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "manifest_sha256")


class HostSessionPreparationV1(_HostWireModel):
    """The secret-free host preparation an adapter records for the one-shot hand-off.

    ``expected_probe_sha256`` pins the probe whose facts the backend recorded;
    the secret delivery refuses a host that now reports different facts.
    """

    session_id: UUID
    adapter_kind: str = Field(pattern=_ADAPTER_KIND_PATTERN)
    workspace_id: str = Field(pattern=_WORKSPACE_ID_PATTERN)
    investigation_id: UUID
    task_id: UUID
    handoff: HostHandoffRefV1
    lease_fence: int = Field(ge=0)
    allowed_workspace_paths: tuple[str, ...] = Field(max_length=HOST_MAX_ALLOWED_PATHS)
    skill_names: tuple[str, ...] = Field(max_length=HOST_MAX_SKILLS)
    budgets: HostBudgetV1
    model: str | None = Field(default=None, pattern=_MODEL_PATTERN)
    network_mode: Literal["isolated_egress"] = "isolated_egress"
    expected_probe_sha256: str

    @field_validator("allowed_workspace_paths")
    @classmethod
    def _validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_workspace_path(item, "allowed_workspace_paths") for item in value)

    @field_validator("skill_names")
    @classmethod
    def _validate_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if re.fullmatch(_SKILL_NAME_PATTERN, item) is None:
                raise ValueError("skill_names entries must name an autowork-* skill")
        return value

    @field_validator("expected_probe_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, "expected_probe_sha256")

    def host_request(self, auth_secret_b64: str) -> HostPrepareSessionRequestV1:
        return HostPrepareSessionRequestV1(
            session_id=self.session_id,
            adapter_kind=self.adapter_kind,
            workspace_id=self.workspace_id,
            investigation_id=self.investigation_id,
            task_id=self.task_id,
            handoff=self.handoff,
            lease_fence=self.lease_fence,
            allowed_workspace_paths=self.allowed_workspace_paths,
            skill_names=self.skill_names,
            budgets=self.budgets,
            model=self.model,
            network_mode=self.network_mode,
            auth_secret=auth_secret_b64,
        )


class HostSessionContextV1(_HostWireModel):
    """Backend coordinates the 05B1 port request does not itself carry.

    A host prepare needs the workspace identity, the handoff reference, the
    task's path/skill envelope and the session budgets.  The backend session
    service owns those facts; this context is the explicit seam an orchestrator
    supplies, never something the adapter guesses.
    """

    workspace_id: str = Field(pattern=_WORKSPACE_ID_PATTERN)
    handoff: HostHandoffRefV1
    lease_fence: int = Field(ge=0)
    allowed_workspace_paths: tuple[str, ...] = Field(max_length=HOST_MAX_ALLOWED_PATHS)
    skill_names: tuple[str, ...] = Field(max_length=HOST_MAX_SKILLS)
    budgets: HostBudgetV1
    model: str | None = Field(default=None, pattern=_MODEL_PATTERN)

    @field_validator("allowed_workspace_paths")
    @classmethod
    def _validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_workspace_path(item, "allowed_workspace_paths") for item in value)

    @field_validator("skill_names")
    @classmethod
    def _validate_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if re.fullmatch(_SKILL_NAME_PATTERN, item) is None:
                raise ValueError("skill_names entries must name an autowork-* skill")
        return value


class HostSessionContextProvider(Protocol):
    """Resolves the host-only coordinates for one 05B1 prepare request.

    ``handoff_id`` is the handoff the session will start from; the provider
    resolves its canonical version and SHA-256 instead of trusting the caller.
    ``requested_model`` is the alias the verified human chose at open; the
    provider validates it and the host contract bounds it.
    """

    async def host_session_context(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        adapter_kind: OperatorAdapterKind,
        handoff_id: UUID,
        requested_model: str | None = None,
    ) -> HostSessionContextV1: ...


class HostSessionsPort(Protocol):
    """The typed ``/v1/sessions/**`` client both operator adapters share."""

    async def probe_capabilities(self, adapter_kind: str) -> HostCapabilityProbeV1: ...

    async def prepare_session(
        self, request: HostPrepareSessionRequestV1, *, idempotency_key: str
    ) -> HostPreparedSessionV1: ...

    async def start_session(
        self,
        session_id: UUID,
        request: HostStartSessionRequestV1,
        *,
        idempotency_key: str,
    ) -> HostStartedSessionV1: ...

    async def get_session_status(self, session_id: UUID) -> HostSessionStatusV1: ...

    async def get_events(
        self,
        session_id: UUID,
        *,
        after: int = 0,
        generation: int | None = None,
        limit: int = HOST_MAX_PAGE_SIZE,
    ) -> HostSessionEventsPageV1: ...

    async def get_logs(
        self, session_id: UUID, *, after: int = 0, limit: int = HOST_MAX_PAGE_SIZE
    ) -> HostLogsPageV1: ...

    async def send_instruction(
        self,
        session_id: UUID,
        request: HostInstructionRequestV1,
        *,
        idempotency_key: str,
    ) -> HostInstructionResultV1: ...

    async def interrupt_session(
        self, session_id: UUID, request: HostReasonRequestV1, *, idempotency_key: str
    ) -> HostInterruptResultV1: ...

    async def resume_session(
        self, session_id: UUID, request: HostResumeRequestV1, *, idempotency_key: str
    ) -> HostStartedSessionV1: ...

    async def reconcile_session(
        self, session_id: UUID, request: HostReconcileRequestV1, *, idempotency_key: str
    ) -> HostReconcileResultV1: ...

    async def collect_results(
        self, session_id: UUID, request: HostCollectRequestV1, *, idempotency_key: str
    ) -> HostSessionResultV1: ...

    async def close_session(
        self, session_id: UUID, request: HostReasonRequestV1, *, idempotency_key: str
    ) -> HostCloseResultV1: ...

    async def set_tool_manifest(
        self,
        session_id: UUID,
        request: HostToolManifestRequestV1,
        *,
        idempotency_key: str,
    ) -> HostToolManifestAcceptedV1: ...


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


__all__ = [
    "HOST_JSON_BODY_LIMIT",
    "HOST_MAX_ALLOWED_PATHS",
    "HOST_MAX_INSTRUCTION_BYTES",
    "HOST_MAX_MANIFEST_TOOLS",
    "HOST_MAX_PAGE_SIZE",
    "HOST_MAX_REASON_CHARS",
    "HOST_MAX_RUN_PROMPT_BYTES",
    "HOST_MAX_SKILLS",
    "HOST_RESPONSE_LIMIT",
    "HOST_SESSIONS_PROTOCOL",
    "HOST_TOOL_MANIFEST_BODY_LIMIT",
    "HostBudgetExceededError",
    "HostBudgetUsageV1",
    "HostBudgetV1",
    "HostCapabilityFlagsV1",
    "HostCapabilityProbeV1",
    "HostCapabilityUnavailableError",
    "HostCapabilityUnsupportedError",
    "HostCapturedFileV1",
    "HostCloseResultV1",
    "HostCollectRequestV1",
    "HostConfinementUnavailableError",
    "HostConfinementV1",
    "HostExitV1",
    "HostHandoffRefV1",
    "HostIdempotencyConflictError",
    "HostInstructionRequestV1",
    "HostInstructionResultV1",
    "HostInterruptResultV1",
    "HostInvalidRequestError",
    "HostLogLineV1",
    "HostLogRangeV1",
    "HostLogsPageV1",
    "HostManifestMissingError",
    "HostPrepareSessionRequestV1",
    "HostPreparedSessionV1",
    "HostReasonRequestV1",
    "HostReconcileEvidenceV1",
    "HostReconcileExitV1",
    "HostReconcileRequestV1",
    "HostReconcileResultV1",
    "HostResumeRequestV1",
    "HostResumeUnavailableError",
    "HostRunExitV1",
    "HostRunV1",
    "HostSecretHandoffConsumedError",
    "HostSecretUnavailableError",
    "HostSessionBusyError",
    "HostSessionContextProvider",
    "HostSessionContextV1",
    "HostSessionContractError",
    "HostSessionEventV1",
    "HostSessionEventsPageV1",
    "HostSessionNotFoundError",
    "HostSessionPreparationV1",
    "HostSessionResultV1",
    "HostSessionStateConflictError",
    "HostSessionStatusV1",
    "HostSessionsError",
    "HostSessionsPort",
    "HostStartSessionRequestV1",
    "HostStartedSessionV1",
    "HostToolManifestAcceptedV1",
    "HostToolManifestEntryV1",
    "HostToolManifestRequestV1",
    "host_error_from_code",
]

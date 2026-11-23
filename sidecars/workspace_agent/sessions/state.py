"""Persisted per-session state, the ADR D2 state machine and typed rejections.

One ``state.json`` per session mirrors the backend-owned ``agent_sessions``
row. It carries ids, adapter kind, state, ``state_version`` CAS, generation,
run number, pid/pgid/start ticks, cursors and timestamps; the session secret is
never written here. Every write is atomic (tmp + fsync + rename + directory
fsync) and every file is 0600.

Normative source: ``docs/design/investigation-workshop/SESSION-RUNNER.md`` §5.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..protocol import ErrorCode

SESSION_STATE_SCHEMA = "autowork-workspace-session.v1"
STATE_FILE_NAME = "state.json"
TEMP_FILE_SUFFIX = ".tmp"
STATE_FILE_MODE = 0o600
MAX_STATE_BYTES = 256 * 1024
SESSION_DIR_MODE = 0o750
SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UUID_PATTERN = SESSION_ID_PATTERN
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SKILL_NAME_PATTERN = re.compile(r"^autowork-[a-z0-9][a-z0-9-]{0,63}$")
MAX_ALLOWED_PATHS = 64
MAX_SKILLS = 32
MAX_SECRET_BYTES = 32
# Typed reason recorded when agent content proves the secret left its socket.
SECRET_EXPOSURE_REASON = "secret_exposure_detected"
# Typed reason recorded when a seeded provider credential copy survives its
# removal at close/stop/interrupt/restart reconciliation (R2B-1).
CREDENTIAL_CLEANUP_REASON = "credential_cleanup_failed"
MAX_BUDGET_WALL_SECONDS = 24 * 60 * 60
MAX_BUDGET_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_BUDGET_INSTRUCTIONS = 10_000


class SessionState(StrEnum):
    """ADR-0011 D2 agent-session states (the host mirrors, never promotes)."""

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


# ADR-0011:64 partial unique index scope: these states keep a task's session
# active. Everything else is terminal and is not touched by restart reconcile.
ACTIVE_STATES: frozenset[SessionState] = frozenset(
    {
        SessionState.PROVISIONING,
        SessionState.READY,
        SessionState.RUNNING,
        SessionState.AWAITING_INSTRUCTION,
        SessionState.INTERRUPT_REQUESTED,
        SessionState.RECONCILING,
    }
)


class SessionError(Exception):
    """A session operation rejected with a protocol code, status and details."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    status: int = 500

    def __init__(self, message: str, *, details: Mapping[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, object] = {} if details is None else dict(details)


class SessionRequestError(SessionError):
    """A request body violates the closed typed contract."""

    code = ErrorCode.INVALID_REQUEST
    status = 400

    def __init__(self, field_name: str, reason: str) -> None:
        super().__init__("invalid session request", details={"field": field_name, "reason": reason})


class SessionNotFoundError(SessionError):
    code = ErrorCode.SESSION_NOT_FOUND
    status = 404

    def __init__(self, session_id: str) -> None:
        super().__init__("session not found", details={"session_id": session_id})


class SessionStateConflictError(SessionError):
    code = ErrorCode.SESSION_STATE_CONFLICT
    status = 409

    def __init__(self, state: str, operation: str) -> None:
        super().__init__(
            "the session state forbids this operation",
            details={"state": state, "operation": operation},
        )


class SessionLostError(SessionError):
    """The session was reconciled ``lost``; INS-10 forbids any later run.

    A restart never adopts a session (its in-memory secret is gone), so an
    instruction addressed to the same id is refused instead of re-delivered.
    """

    code = ErrorCode.SESSION_STATE_CONFLICT
    status = 409

    def __init__(self, operation: str) -> None:
        super().__init__(
            "the session was lost and cannot accept this operation",
            details={
                "state": str(SessionState.LOST),
                "operation": operation,
                "reason": "session_lost",
            },
        )


class SessionBusyError(SessionError):
    code = ErrorCode.SESSION_BUSY
    status = 409

    def __init__(self, session_id: str) -> None:
        super().__init__(
            "the session already has an active run", details={"session_id": session_id}
        )


class ConfinementUnavailableError(SessionError):
    code = ErrorCode.CONFINEMENT_UNAVAILABLE
    status = 503

    def __init__(self, adapter_kind: str, *, reason: str | None = None) -> None:
        details: dict[str, object] = {"adapter_kind": adapter_kind}
        if reason:
            details["reason"] = reason
        super().__init__("the confinement profile is unavailable", details=details)


class CapabilityUnavailableError(SessionError):
    code = ErrorCode.CAPABILITY_UNAVAILABLE
    status = 409

    def __init__(self, adapter_kind: str) -> None:
        super().__init__(
            "the adapter is not registered or not supported", details={"adapter_kind": adapter_kind}
        )


class ResumeUnavailableError(SessionError):
    code = ErrorCode.RESUME_UNAVAILABLE
    status = 409

    def __init__(self, adapter_kind: str) -> None:
        super().__init__(
            "the adapter did not prove resume support", details={"adapter_kind": adapter_kind}
        )


class IdempotencyConflictError(SessionError):
    code = ErrorCode.IDEMPOTENCY_CONFLICT
    status = 409

    def __init__(self, key: str) -> None:
        super().__init__(
            "the Idempotency-Key was reused with a different request",
            details={"idempotency_key": key},
        )


class BudgetExceededError(SessionError):
    code = ErrorCode.BUDGET_EXCEEDED
    status = 409

    def __init__(self, budget: str) -> None:
        super().__init__("a session budget is exhausted", details={"budget": budget})


class ManifestMissingError(SessionError):
    code = ErrorCode.MANIFEST_MISSING
    status = 409

    def __init__(self) -> None:
        super().__init__("the session has no accepted tool manifest")


class SecretUnavailableError(SessionError):
    code = ErrorCode.SECRET_UNAVAILABLE
    status = 409

    def __init__(self, *, reason: str | None = None) -> None:
        details: dict[str, object] = {} if reason is None else {"reason": reason}
        super().__init__("the session secret handoff cannot be armed", details=details)


class SecretExposureError(SessionError):
    """Agent content carried the session secret: the request is refused (F2).

    Reaching this error means the secret left its one-shot socket, so the
    session is revoked (channels closed, secret wiped, state ``failed``).
    Neither the message nor the details ever carry the offending content.
    """

    code = ErrorCode.POLICY_DENIED
    status = 403

    def __init__(self, *, operation: str) -> None:
        super().__init__(
            "agent content contains the session secret",
            details={"reason": SECRET_EXPOSURE_REASON, "operation": operation},
        )


class CredentialCleanupError(SessionError):
    """A seeded provider credential copy survives its removal (R2B-1).

    The session is failed before this is raised: a host that cannot delete the
    copy must neither present the session as cleanly closed nor let it be
    reused.  Neither the message nor the details carry the credential values.
    """

    code = ErrorCode.INTERNAL_ERROR
    status = 500

    def __init__(self, *, operation: str) -> None:
        super().__init__(
            "the session provider credential copy cannot be removed",
            details={"reason": CREDENTIAL_CLEANUP_REASON, "operation": operation},
        )


class CursorExpiredError(SessionError):
    """A cursor fell out of the retained ring or belongs to another generation."""

    code = ErrorCode.CURSOR_EXPIRED
    status = 409

    def __init__(self, *, generation: int, oldest_retained: int, last_seq: int, after: int) -> None:
        super().__init__(
            "session cursor expired; resynchronise from the status",
            details={
                "resync_required": True,
                "generation": generation,
                "oldest_retained": oldest_retained,
                "last_seq": last_seq,
                "after": after,
            },
        )


@dataclass(frozen=True)
class HandoffRef:
    handoff_id: str
    version: int
    sha256: str

    def to_payload(self) -> dict[str, object]:
        return {"handoff_id": self.handoff_id, "version": self.version, "sha256": self.sha256}

    @classmethod
    def from_payload(cls, payload: object) -> HandoffRef:
        entry = require_object(payload, "handoff")
        require_fields(entry, ("handoff_id", "version", "sha256"), ())
        handoff_id = require_uuid(entry["handoff_id"], "handoff.handoff_id")
        version = require_int(entry["version"], "handoff.version", minimum=1, maximum=1_000_000)
        sha256 = require_sha256(entry["sha256"], "handoff.sha256")
        return cls(handoff_id=handoff_id, version=version, sha256=sha256)


@dataclass(frozen=True)
class BudgetSpec:
    wall_seconds: int
    max_output_bytes: int
    max_instructions: int

    def to_payload(self) -> dict[str, int]:
        return {
            "wall_seconds": self.wall_seconds,
            "max_output_bytes": self.max_output_bytes,
            "max_instructions": self.max_instructions,
        }

    @classmethod
    def from_payload(cls, payload: object) -> BudgetSpec:
        entry = require_object(payload, "budgets")
        require_fields(entry, ("wall_seconds", "max_output_bytes", "max_instructions"), ())
        return cls(
            wall_seconds=require_int(
                entry["wall_seconds"],
                "budgets.wall_seconds",
                minimum=1,
                maximum=MAX_BUDGET_WALL_SECONDS,
            ),
            max_output_bytes=require_int(
                entry["max_output_bytes"],
                "budgets.max_output_bytes",
                minimum=1024,
                maximum=MAX_BUDGET_OUTPUT_BYTES,
            ),
            max_instructions=require_int(
                entry["max_instructions"],
                "budgets.max_instructions",
                minimum=1,
                maximum=MAX_BUDGET_INSTRUCTIONS,
            ),
        )


@dataclass
class BudgetUsage:
    wall_seconds: float = 0.0
    output_bytes: int = 0
    instructions: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "wall_seconds": round(self.wall_seconds, 3),
            "output_bytes": self.output_bytes,
            "instructions": self.instructions,
        }

    @classmethod
    def from_payload(cls, payload: object) -> BudgetUsage:
        if payload is None:
            return cls()
        entry = require_object(payload, "budgets_used")
        require_fields(entry, ("wall_seconds", "output_bytes", "instructions"), ())
        wall = entry["wall_seconds"]
        if isinstance(wall, bool) or not isinstance(wall, (int, float)) or wall < 0:
            raise SessionError("invalid persisted budget usage")
        return cls(
            wall_seconds=float(wall),
            output_bytes=require_int(entry["output_bytes"], "budgets_used.output_bytes", minimum=0),
            instructions=require_int(entry["instructions"], "budgets_used.instructions", minimum=0),
        )


@dataclass(frozen=True)
class ExitRecord:
    run: int
    code: int | None
    signal: int | None
    at: str

    def to_payload(self) -> dict[str, object]:
        return {"run": self.run, "code": self.code, "signal": self.signal, "at": self.at}

    @classmethod
    def from_payload(cls, payload: object) -> ExitRecord:
        entry = require_object(payload, "exit")
        require_fields(entry, ("run", "code", "signal", "at"), ())
        code = entry["code"]
        signal_number = entry["signal"]
        return cls(
            run=require_int(entry["run"], "exit.run", minimum=1),
            code=None if code is None else require_int(code, "exit.code", minimum=0),
            signal=None
            if signal_number is None
            else require_int(signal_number, "exit.signal", minimum=1, maximum=64),
            at=require_str(entry["at"], "exit.at"),
        )


@dataclass
class RunRecord:
    run: int
    state: str
    pid: int | None
    pgid: int | None
    start_ticks: int | None
    started_at: str
    log_start: int = 0
    ended_at: str | None = None
    exit: ExitRecord | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "run": self.run,
            "state": self.state,
            "pid": self.pid,
            "pgid": self.pgid,
            "start_ticks": self.start_ticks,
            "started_at": self.started_at,
            "log_start": self.log_start,
            "ended_at": self.ended_at,
            "exit": None if self.exit is None else self.exit.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> RunRecord:
        entry = require_object(payload, "run")
        require_fields(
            entry,
            (
                "run",
                "state",
                "pid",
                "pgid",
                "start_ticks",
                "started_at",
                "log_start",
                "ended_at",
                "exit",
            ),
            (),
        )
        exit_payload = entry["exit"]
        return cls(
            run=require_int(entry["run"], "run.run", minimum=1),
            state=require_str(entry["state"], "run.state"),
            pid=None if entry["pid"] is None else require_int(entry["pid"], "run.pid", minimum=1),
            pgid=None
            if entry["pgid"] is None
            else require_int(entry["pgid"], "run.pgid", minimum=2),
            start_ticks=None
            if entry["start_ticks"] is None
            else require_int(entry["start_ticks"], "run.start_ticks", minimum=0),
            started_at=require_str(entry["started_at"], "run.started_at"),
            log_start=require_int(entry["log_start"], "run.log_start", minimum=0),
            ended_at=None
            if entry["ended_at"] is None
            else require_str(entry["ended_at"], "run.ended_at"),
            exit=None if exit_payload is None else ExitRecord.from_payload(exit_payload),
        )


@dataclass
class SessionRecord:
    session_id: str
    adapter_kind: str
    workspace_id: str
    investigation_id: str
    task_id: str
    handoff: HandoffRef
    lease_fence: int
    allowed_workspace_paths: tuple[str, ...]
    skill_names: tuple[str, ...]
    model: str | None
    prompt_sha256: str
    capability_sha256: str
    confinement: dict[str, object]
    budgets: BudgetSpec
    state: SessionState
    state_version: int
    generation: int
    runs: list[RunRecord] = field(default_factory=list)
    log_seq: int = 0
    event_seq: int = 0
    budgets_used: BudgetUsage = field(default_factory=BudgetUsage)
    created_at: str = ""
    updated_at: str = ""
    last_activity_at: str = ""
    stopped_at: str | None = None
    exit: ExitRecord | None = None
    outcome: str | None = None
    manifest_sha256: str | None = None
    captured: dict[str, str] = field(default_factory=dict)
    checkpoint_sha256: str | None = None
    close_reason: str | None = None
    reconciliation: dict[str, object] | None = None

    @property
    def current_run(self) -> int:
        return self.runs[-1].run if self.runs else 0

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": SESSION_STATE_SCHEMA,
            "session_id": self.session_id,
            "adapter_kind": self.adapter_kind,
            "workspace_id": self.workspace_id,
            "investigation_id": self.investigation_id,
            "task_id": self.task_id,
            "handoff": self.handoff.to_payload(),
            "lease_fence": self.lease_fence,
            "allowed_workspace_paths": list(self.allowed_workspace_paths),
            "skill_names": list(self.skill_names),
            "model": self.model,
            "prompt_sha256": self.prompt_sha256,
            "capability_sha256": self.capability_sha256,
            "confinement": dict(self.confinement),
            "budgets": self.budgets.to_payload(),
            "state": str(self.state),
            "state_version": self.state_version,
            "generation": self.generation,
            "runs": [run.to_payload() for run in self.runs],
            "log_seq": self.log_seq,
            "event_seq": self.event_seq,
            "budgets_used": self.budgets_used.to_payload(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity_at": self.last_activity_at,
            "stopped_at": self.stopped_at,
            "exit": None if self.exit is None else self.exit.to_payload(),
            "outcome": self.outcome,
            "manifest_sha256": self.manifest_sha256,
            "captured": dict(self.captured),
            "checkpoint_sha256": self.checkpoint_sha256,
            "close_reason": self.close_reason,
            "reconciliation": None if self.reconciliation is None else dict(self.reconciliation),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> SessionRecord:
        if set(payload) != STATE_FIELDS:
            raise SessionError("persisted session state has unexpected fields")
        if payload["schema"] != SESSION_STATE_SCHEMA:
            raise SessionError("persisted session state has an unexpected schema")
        state_text = require_str(payload["state"], "state")
        try:
            state = SessionState(state_text)
        except ValueError as exc:
            raise SessionError("persisted session state is unknown") from exc
        raw_runs = payload["runs"]
        if not isinstance(raw_runs, list) or len(raw_runs) > 1000:
            raise SessionError("persisted session runs are invalid")
        raw_captured = payload["captured"]
        if not isinstance(raw_captured, dict):
            raise SessionError("persisted captured files are invalid")
        reconciliation = payload["reconciliation"]
        exit_payload = payload["exit"]
        model = payload["model"]
        return cls(
            session_id=require_uuid(payload["session_id"], "session_id"),
            adapter_kind=require_str(payload["adapter_kind"], "adapter_kind"),
            workspace_id=require_str(payload["workspace_id"], "workspace_id"),
            investigation_id=require_uuid(payload["investigation_id"], "investigation_id"),
            task_id=require_uuid(payload["task_id"], "task_id"),
            handoff=HandoffRef.from_payload(payload["handoff"]),
            lease_fence=require_int(payload["lease_fence"], "lease_fence", minimum=0),
            allowed_workspace_paths=require_str_tuple(
                payload["allowed_workspace_paths"], "allowed_workspace_paths"
            ),
            skill_names=require_str_tuple(
                payload["skill_names"], "skill_names", maximum=MAX_SKILLS
            ),
            model=None if model is None else require_str(model, "model"),
            prompt_sha256=require_sha256(payload["prompt_sha256"], "prompt_sha256"),
            capability_sha256=require_sha256(payload["capability_sha256"], "capability_sha256"),
            confinement=require_object(payload["confinement"], "confinement"),
            budgets=BudgetSpec.from_payload(payload["budgets"]),
            state=state,
            state_version=require_int(payload["state_version"], "state_version", minimum=1),
            generation=require_int(payload["generation"], "generation", minimum=1),
            runs=[RunRecord.from_payload(entry) for entry in raw_runs],
            log_seq=require_int(payload["log_seq"], "log_seq", minimum=0),
            event_seq=require_int(payload["event_seq"], "event_seq", minimum=0),
            budgets_used=BudgetUsage.from_payload(payload["budgets_used"]),
            created_at=require_str(payload["created_at"], "created_at"),
            updated_at=require_str(payload["updated_at"], "updated_at"),
            last_activity_at=require_str(payload["last_activity_at"], "last_activity_at"),
            stopped_at=None
            if payload["stopped_at"] is None
            else require_str(payload["stopped_at"], "stopped_at"),
            exit=None if exit_payload is None else ExitRecord.from_payload(exit_payload),
            outcome=None
            if payload["outcome"] is None
            else require_str(payload["outcome"], "outcome"),
            manifest_sha256=None
            if payload["manifest_sha256"] is None
            else require_sha256(payload["manifest_sha256"], "manifest_sha256"),
            captured={
                str(key): require_str(value, "captured") for key, value in raw_captured.items()
            },
            checkpoint_sha256=None
            if payload["checkpoint_sha256"] is None
            else require_sha256(payload["checkpoint_sha256"], "checkpoint_sha256"),
            close_reason=None
            if payload["close_reason"] is None
            else require_str(payload["close_reason"], "close_reason"),
            reconciliation=None
            if reconciliation is None
            else require_object(reconciliation, "reconciliation"),
        )


STATE_FIELDS: frozenset[str] = frozenset(
    {
        "schema",
        "session_id",
        "adapter_kind",
        "workspace_id",
        "investigation_id",
        "task_id",
        "handoff",
        "lease_fence",
        "allowed_workspace_paths",
        "skill_names",
        "model",
        "prompt_sha256",
        "capability_sha256",
        "confinement",
        "budgets",
        "state",
        "state_version",
        "generation",
        "runs",
        "log_seq",
        "event_seq",
        "budgets_used",
        "created_at",
        "updated_at",
        "last_activity_at",
        "stopped_at",
        "exit",
        "outcome",
        "manifest_sha256",
        "captured",
        "checkpoint_sha256",
        "close_reason",
        "reconciliation",
    }
)


def require_object(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SessionRequestError(field_name, "object_required")
    return {str(key): item for key, item in value.items()}


def require_fields(
    payload: Mapping[str, object],
    required: Sequence[str],
    optional: Sequence[str],
) -> None:
    """Reject missing, unknown or duplicated (JSON forbids duplicates) fields."""
    allowed = set(required) | set(optional)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise SessionRequestError(unknown[0], "unknown_field")
    for name in required:
        if name not in payload:
            raise SessionRequestError(name, "missing_field")


def require_str(value: object, field_name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise SessionRequestError(field_name, "string_required")
    return value


def require_int(
    value: object, field_name: str, *, minimum: int = 0, maximum: int = 2**63 - 1
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SessionRequestError(field_name, "integer_out_of_range")
    return value


def require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise SessionRequestError(field_name, "boolean_required")
    return value


def require_uuid(value: object, field_name: str) -> str:
    text = require_str(value, field_name, maximum=36)
    if UUID_PATTERN.fullmatch(text) is None:
        raise SessionRequestError(field_name, "uuid_required")
    return text


def require_sha256(value: object, field_name: str) -> str:
    text = require_str(value, field_name, maximum=64)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise SessionRequestError(field_name, "sha256_required")
    return text


def require_token(value: object, field_name: str) -> str:
    text = require_str(value, field_name, maximum=128)
    if TOKEN_PATTERN.fullmatch(text) is None:
        raise SessionRequestError(field_name, "token_required")
    return text


def require_str_tuple(
    value: object, field_name: str, *, maximum: int = MAX_ALLOWED_PATHS
) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SessionRequestError(field_name, "string_list_required")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SessionRequestError(field_name, "string_list_required")
        items.append(item)
    return tuple(items)


def decode_session_secret(value: object) -> bytes:
    """Decode the one-time base64 ``auth_secret``; require exactly 32 bytes."""
    text = require_str(value, "auth_secret", maximum=128)
    try:
        secret = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise SessionRequestError("auth_secret", "base64_required") from exc
    if len(secret) != MAX_SECRET_BYTES:
        raise SessionRequestError("auth_secret", "wrong_length")
    return secret


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Write one 0600 JSON document atomically inside its real parent directory."""
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_STATE_BYTES:
        raise SessionError("session state exceeds its byte budget")
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = f".{path.name}.{uuid.uuid4().hex}{TEMP_FILE_SUFFIX}"
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            STATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        try:
            os.fchmod(fd, STATE_FILE_MODE)
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(parent_fd)


def read_json(path: Path) -> dict[str, object]:
    """Read one bounded 0600 JSON object, refusing links and non-regular files."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise SessionError("session state cannot be opened") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SessionError("session state is not a regular file")
        if info.st_size > MAX_STATE_BYTES:
            raise SessionError("session state exceeds its byte budget")
        chunks: list[bytes] = []
        remaining = MAX_STATE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    body = b"".join(chunks)
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionError("session state is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SessionError("session state is not a JSON object")
    return {str(key): item for key, item in value.items()}


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        remaining = remaining[written:]

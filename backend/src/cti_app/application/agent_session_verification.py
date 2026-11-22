"""D7 agent-session authentication: secret derivation and request HMAC.

The session secret is derived server-side, stored as a nonce plus its SHA-256,
and handed to the host operator exactly once.  The host signs every M24
operator request with it; the server re-derives the same secret from
``M24_AGENT_SESSION_KEY`` and the stored nonce, so nothing secret is ever
persisted and the signature is verified without a bearer token.

Message (ADR-0011 D7)::

    method \\0 path+query \\0 session_id \\0 sha256(body) \\0 idempotency_key \\0 timestamp

with ``v1=<hex>`` HMAC-SHA256, ``compare_digest`` and a ±300 s timestamp
window.  Verification then requires an *active* session bound to the route's
investigation (and task), and, for a write, the session's recorded task lease
to still be the current one.

This module is the fail-closed default lot 04B3 mounts: without
``M24_AGENT_SESSION_KEY`` every verification and every prepare fails with the
typed ``503 agent_sessions_unavailable``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from types import TracebackType
from typing import Protocol
from uuid import UUID

from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    InvestigationTaskRepository,
)
from cti_app.domain.agentic_operator import (
    AGENT_SESSION_SECRET_NONCE_BYTES,
    OperatorAdapterKind,
)
from cti_app.domain.investigation_task import InvestigationTaskV1, TaskStatus

SESSION_SECRET_CONTEXT = b"autowork-agent-session.v1\0"
AGENT_SESSION_SIGNATURE_VERSION = "v1"
AGENT_SESSION_TIMESTAMP_SKEW_SECONDS = 300
AGENT_SESSION_KEY_BYTES = 32

_SIGNATURE_PATTERN = re.compile(r"^v1=([0-9a-f]{64})$")
_SAFE_PATH_SEGMENT = re.compile(r"^[!-~]*$")


class AgentSessionVerificationError(ValueError):
    """Base refusal for a session-authenticated request, HTTP-stable."""

    code = "agent_session_required"
    status_code = 403

    def __init__(self, message: str, *, details: dict[str, str] | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


class AgentSessionsUnavailableError(AgentSessionVerificationError):
    """The server key is absent: agent sessions are fail-closed (D7)."""

    code = "agent_sessions_unavailable"
    status_code = 503


class AgentSessionRequiredError(AgentSessionVerificationError):
    """The three D7 headers are not all present and well formed."""

    code = "agent_session_required"


class AgentSessionSignatureError(AgentSessionVerificationError):
    """The HMAC, timestamp or session secret does not verify."""

    code = "agent_session_signature_invalid"


class AgentSessionInactiveError(AgentSessionVerificationError):
    """The session exists but is not in an active technical state."""

    code = "agent_session_inactive"
    status_code = 409


class AgentSessionBindingError(AgentSessionVerificationError):
    """The session is not bound to the route's investigation or task."""

    code = "agent_session_binding_mismatch"


class AgentSessionLeaseError(AgentSessionVerificationError):
    """A write arrives with a stale or superseded task lease fence."""

    code = "agent_session_lease_stale"
    status_code = 409


def session_secret_hex(secret: bytes) -> str:
    if not isinstance(secret, bytes) or len(secret) != hashlib.sha256().digest_size:
        raise ValueError("a session secret is a 32-byte HMAC output")
    return secret.hex()


def session_secret_hash(secret: bytes) -> str:
    return hashlib.sha256(secret).hexdigest()


def _session_key_bytes(key: object) -> bytes:
    if isinstance(key, str):
        raw = key.encode("utf-8")
    elif isinstance(key, bytes):
        raw = key
    else:
        secret_value = getattr(key, "get_secret_value", None)
        if not callable(secret_value):
            raise TypeError("M24_AGENT_SESSION_KEY must be a string, bytes or SecretStr")
        raw = str(secret_value()).encode("utf-8")
    if len(raw) != AGENT_SESSION_KEY_BYTES:
        raise ValueError("M24_AGENT_SESSION_KEY must be exactly 32 bytes")
    return raw


class SessionSecretDeriver(Protocol):
    """Derives one session secret from the server key and the stored nonce."""

    def derive(self, *, session_id: UUID, nonce: bytes) -> bytes: ...


class HmacSessionSecretDeriver:
    """``HMAC-SHA256(key, "autowork-agent-session.v1\\0" + session_id + "\\0" + nonce)``."""

    def __init__(self, key: object) -> None:
        raw = _session_key_bytes(key)
        self._key = raw
        self._lock = Lock()

    def derive(self, *, session_id: UUID, nonce: bytes) -> bytes:
        if not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID")
        if not isinstance(nonce, bytes) or len(nonce) != AGENT_SESSION_SECRET_NONCE_BYTES:
            raise ValueError("nonce must be 32 bytes")
        message = SESSION_SECRET_CONTEXT + str(session_id).encode("ascii") + b"\0" + nonce
        with self._lock:
            return hmac.new(self._key, message, hashlib.sha256).digest()


def build_agent_session_message(
    *,
    method: str,
    path_and_query: str,
    session_id: UUID,
    body: bytes,
    idempotency_key: str | None,
    timestamp: str,
) -> bytes:
    """The exact D7 message a host operator signs and the server recomputes."""
    if not isinstance(method, str) or not method.strip():
        raise ValueError("method must be a non-empty HTTP method")
    if not isinstance(path_and_query, str) or not path_and_query.startswith("/"):
        raise ValueError("path_and_query must start with '/'")
    if not _SAFE_PATH_SEGMENT.fullmatch(path_and_query):
        raise ValueError("path_and_query must be printable ASCII without spaces")
    if not isinstance(session_id, UUID):
        raise TypeError("session_id must be a UUID")
    if not isinstance(body, bytes):
        raise TypeError("body must be raw bytes")
    if not isinstance(timestamp, str) or not timestamp.isascii() or not timestamp.isdecimal():
        raise ValueError("timestamp must be a decimal unix timestamp")
    key = "" if idempotency_key is None else idempotency_key
    if not isinstance(key, str):
        raise TypeError("idempotency_key must be a string or None")
    return b"\0".join(
        (
            method.strip().upper().encode("ascii"),
            path_and_query.encode("ascii"),
            str(session_id).encode("ascii"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
            key.encode("utf-8"),
            timestamp.encode("ascii"),
        )
    )


def parse_agent_session_headers(
    headers: Mapping[str, str], *, now: datetime | None = None
) -> tuple[UUID, str, str]:
    """Return ``(session_id, timestamp, signature_hex)`` or raise a typed 403."""
    session_header = headers.get("x-autowork-agent-session")
    timestamp_header = headers.get("x-autowork-agent-timestamp")
    signature_header = headers.get("x-autowork-agent-signature")
    if not session_header or not timestamp_header or not signature_header:
        raise AgentSessionRequiredError(
            "X-AutoWork-Agent-Session, -Timestamp and -Signature are all required"
        )
    try:
        session_id = UUID(session_header.strip())
    except (TypeError, ValueError) as exc:
        raise AgentSessionSignatureError("the agent session header is not a UUID") from exc
    if str(session_id) != session_header.strip():
        raise AgentSessionSignatureError("the agent session header is not a canonical UUID")
    timestamp = timestamp_header.strip()
    if not timestamp.isascii() or not timestamp.isdecimal():
        raise AgentSessionSignatureError("the agent timestamp is not a decimal unix time")
    if len(timestamp) > 12:
        raise AgentSessionSignatureError("the agent timestamp is out of range")
    match = _SIGNATURE_PATTERN.fullmatch(signature_header.strip())
    if match is None:
        raise AgentSessionSignatureError("the agent signature is not v1=<64 hex>")
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        signed_at = int(timestamp)
    except ValueError as exc:  # pragma: no cover - decimal digits were already checked
        raise AgentSessionSignatureError("the agent timestamp is invalid") from exc
    if abs(current.timestamp() - signed_at) > AGENT_SESSION_TIMESTAMP_SKEW_SECONDS:
        raise AgentSessionSignatureError("the agent timestamp is outside the ±300 s window")
    return session_id, timestamp, match.group(1)


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedAgentSession:
    """A verified session and the exact task attribution it may act for."""

    session_id: UUID
    investigation_id: UUID
    task_id: UUID
    adapter_kind: OperatorAdapterKind
    task_lease_fence: int | None


class AgentSessionVerificationUnitOfWork(Protocol):
    agent_sessions: AgentSessionRepository
    investigation_tasks: InvestigationTaskRepository

    async def __aenter__(self) -> AgentSessionVerificationUnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class AgentSessionVerifier(Protocol):
    """The session-verification seam mounted by operator routes (D7)."""

    @property
    def available(self) -> bool: ...

    async def verify_session_request(
        self,
        *,
        method: str,
        path_and_query: str,
        session_id: UUID,
        timestamp: str,
        signature: str,
        body: bytes,
        idempotency_key: str | None,
        investigation_id: UUID,
        task_id: UUID | None = None,
        require_current_lease: bool = False,
        now: datetime | None = None,
    ) -> VerifiedAgentSession: ...


class HmacAgentSessionVerifier:
    """Real D7 verifier: derives the secret from the nonce, then checks everything.

    ``deriver is None`` means ``M24_AGENT_SESSION_KEY`` is absent; every call
    raises :class:`AgentSessionsUnavailableError` (fail-closed, never admits a
    request because the key is missing).
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], AgentSessionVerificationUnitOfWork],
        deriver: SessionSecretDeriver | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._deriver = deriver
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def available(self) -> bool:
        return self._deriver is not None

    async def verify_session_request(
        self,
        *,
        method: str,
        path_and_query: str,
        session_id: UUID,
        timestamp: str,
        signature: str,
        body: bytes,
        idempotency_key: str | None,
        investigation_id: UUID,
        task_id: UUID | None = None,
        require_current_lease: bool = False,
        now: datetime | None = None,
    ) -> VerifiedAgentSession:
        if self._deriver is None:
            raise AgentSessionsUnavailableError(
                "M24_AGENT_SESSION_KEY is not configured; agent sessions are disabled"
            )
        current = now if now is not None else self._clock()
        if not isinstance(timestamp, str) or not timestamp.isascii() or not timestamp.isdecimal():
            raise AgentSessionSignatureError("the agent timestamp is not a decimal unix time")
        if len(timestamp) > 12:
            raise AgentSessionSignatureError("the agent timestamp is out of range")
        if abs(current.timestamp() - int(timestamp)) > AGENT_SESSION_TIMESTAMP_SKEW_SECONDS:
            raise AgentSessionSignatureError("the agent timestamp is outside the ±300 s window")
        supplied = signature[3:] if signature.startswith("v1=") else signature
        if not re.fullmatch(r"[0-9a-f]{64}", supplied):
            raise AgentSessionSignatureError("the agent signature is not a 64-hex digest")
        message = build_agent_session_message(
            method=method,
            path_and_query=path_and_query,
            session_id=session_id,
            body=body,
            idempotency_key=idempotency_key,
            timestamp=timestamp,
        )
        async with self._uow_factory() as uow:
            session = await uow.agent_sessions.get(session_id)
            if session is None:
                # Do not distinguish an unknown session from a bad signature.
                raise AgentSessionSignatureError("the agent session does not verify")
            secret = self._deriver.derive(session_id=session.id, nonce=session.auth_secret_nonce)
            if not hmac.compare_digest(session_secret_hash(secret), session.auth_secret_hash):
                raise AgentSessionSignatureError("the agent session does not verify")
            expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, supplied):
                raise AgentSessionSignatureError("the agent session does not verify")
            if session.investigation_id != investigation_id:
                raise AgentSessionBindingError("the session belongs to another investigation")
            if task_id is not None and session.task_id != task_id:
                raise AgentSessionBindingError("the session belongs to another task")
            if not session.is_active:
                raise AgentSessionInactiveError(
                    f"the agent session is {session.state.value}, not active"
                )
            if require_current_lease:
                task = await uow.investigation_tasks.get(session.task_id)
                self._require_current_lease(task, session.id, session.task_lease_fence, current)
            return VerifiedAgentSession(
                session_id=session.id,
                investigation_id=session.investigation_id,
                task_id=session.task_id,
                adapter_kind=session.adapter_kind,
                task_lease_fence=session.task_lease_fence,
            )

    @staticmethod
    def _require_current_lease(
        task: InvestigationTaskV1 | None,
        session_id: UUID,
        session_fence: int | None,
        now: datetime,
    ) -> None:
        # ``session.session_id`` is kept as a parameter name for readability at
        # the call site; the three facts that must all agree follow.
        if task is None:
            raise AgentSessionLeaseError("the bound task no longer exists")
        if (
            session_fence is None
            or task.lease_session_id != session_id
            or task.lease_fence != session_fence
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
            or task.status not in (TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT)
        ):
            raise AgentSessionLeaseError("the task lease is expired or superseded")


__all__ = [
    "AGENT_SESSION_KEY_BYTES",
    "AGENT_SESSION_SIGNATURE_VERSION",
    "AGENT_SESSION_TIMESTAMP_SKEW_SECONDS",
    "SESSION_SECRET_CONTEXT",
    "AgentSessionBindingError",
    "AgentSessionInactiveError",
    "AgentSessionLeaseError",
    "AgentSessionRequiredError",
    "AgentSessionSignatureError",
    "AgentSessionVerificationError",
    "AgentSessionVerificationUnitOfWork",
    "AgentSessionVerifier",
    "AgentSessionsUnavailableError",
    "HmacAgentSessionVerifier",
    "HmacSessionSecretDeriver",
    "SessionSecretDeriver",
    "VerifiedAgentSession",
    "build_agent_session_message",
    "parse_agent_session_headers",
    "session_secret_hash",
    "session_secret_hex",
]

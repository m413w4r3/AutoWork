"""Session-authentication seam for the M24 operator routes (ADR-0011 D7).

Lot 05B1 owns ``agent_sessions``, the per-session HMAC secret and the real
verifier.  The 04B3 task routes must already bind an operator call to a
verified session, so this module defines only the narrow port they call:

* :class:`AgentSessionVerifier` turns the ``X-AutoWork-Agent-Session`` header
  into a :class:`AgentSessionBinding` (session, investigation, task, operator
  attribution) or refuses it.
* :class:`RefusingAgentSessionVerifier` is the fail-closed default: until
  05B1 lands there is no way to authenticate an operator, so every operator
  call is refused instead of being attributed to the shared
  ``OPERATOR_CLIENT`` token.
* :class:`InMemoryAgentSessionVerifier` is the test fake.

The HMAC signature headers (``X-AutoWork-Agent-Timestamp`` /
``X-AutoWork-Agent-Signature``) and the ``M24_AGENT_SESSION_KEY`` belong to
05B1; the port accepts to be composed with them without changing the routes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSessionBinding:
    """A server-derived, active session bound to one task of an investigation.

    ``operator_id`` is the attribution the task engine compares with
    ``investigation_tasks.assigned_actor_id``; it is never taken from a
    request body.
    """

    session_id: UUID
    investigation_id: UUID
    task_id: UUID | None
    operator_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, UUID):
            raise TypeError("session_id must be a UUID")
        if not isinstance(self.investigation_id, UUID):
            raise TypeError("investigation_id must be a UUID")
        if self.task_id is not None and not isinstance(self.task_id, UUID):
            raise TypeError("task_id must be a UUID or None")
        if not isinstance(self.operator_id, str) or not self.operator_id.strip():
            raise ValueError("operator_id must not be empty")


class AgentSessionVerifier(Protocol):
    """Resolve a session header to its binding, or ``None`` when unverified."""

    async def verify(
        self, *, session_id: UUID, investigation_id: UUID
    ) -> AgentSessionBinding | None: ...


class RefusingAgentSessionVerifier:
    """Fail-closed default used until lot 05B1 provides session secrets."""

    async def verify(
        self, *, session_id: UUID, investigation_id: UUID
    ) -> AgentSessionBinding | None:
        del session_id, investigation_id
        return None


class InMemoryAgentSessionVerifier:
    """Test fake: only explicitly bound sessions verify."""

    def __init__(self, bindings: Iterable[AgentSessionBinding] = ()) -> None:
        self._bindings: dict[UUID, AgentSessionBinding] = {
            binding.session_id: binding for binding in bindings
        }

    def bind(self, binding: AgentSessionBinding) -> None:
        self._bindings[binding.session_id] = binding

    async def verify(
        self, *, session_id: UUID, investigation_id: UUID
    ) -> AgentSessionBinding | None:
        binding = self._bindings.get(session_id)
        if binding is None or binding.investigation_id != investigation_id:
            return None
        return binding


__all__ = [
    "AgentSessionBinding",
    "AgentSessionVerifier",
    "InMemoryAgentSessionVerifier",
    "RefusingAgentSessionVerifier",
]

"""Compatibility façade for the single D7 session-auth port.

The port, the real HMAC verifier, the fail-closed default and the test fake are
all defined once in :mod:`cti_app.application.agent_session_verifier`.  This
module keeps the lot-05B1 import path working without defining a second
``AgentSessionVerifier`` protocol; new code should import the canonical module.
"""

from __future__ import annotations

from cti_app.application.agent_session_verifier import (
    AGENT_SESSION_KEY_BYTES,
    AGENT_SESSION_SIGNATURE_VERSION,
    AGENT_SESSION_TIMESTAMP_SKEW_SECONDS,
    SESSION_SECRET_CONTEXT,
    AgentSessionBinding,
    AgentSessionBindingError,
    AgentSessionInactiveError,
    AgentSessionLeaseError,
    AgentSessionRequiredError,
    AgentSessionSignatureError,
    AgentSessionsUnavailableError,
    AgentSessionVerificationError,
    AgentSessionVerificationUnitOfWork,
    AgentSessionVerifier,
    HmacAgentSessionVerifier,
    HmacSessionSecretDeriver,
    InMemoryAgentSessionVerifier,
    RefusingAgentSessionVerifier,
    SessionSecretDeriver,
    VerifiedAgentSession,
    build_agent_session_message,
    parse_agent_session_headers,
    session_secret_hash,
    session_secret_hex,
)

__all__ = [
    "AGENT_SESSION_KEY_BYTES",
    "AGENT_SESSION_SIGNATURE_VERSION",
    "AGENT_SESSION_TIMESTAMP_SKEW_SECONDS",
    "SESSION_SECRET_CONTEXT",
    "AgentSessionBinding",
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
    "InMemoryAgentSessionVerifier",
    "RefusingAgentSessionVerifier",
    "SessionSecretDeriver",
    "VerifiedAgentSession",
    "build_agent_session_message",
    "parse_agent_session_headers",
    "session_secret_hash",
    "session_secret_hex",
]

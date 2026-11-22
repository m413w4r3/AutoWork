"""D01 contract oracles for the agentic operator port (ADR-0011 D7).

The probe contract must report only facts a real command established: a mode
absent from every recorded probe is refused, a capability flag needs a
successful probe, and the capability hash is derived from the exact payload.
The session machine accepts exactly the D7 states and refuses undeclared
transitions.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cti_app.domain.agentic_operator import (
    ACTIVE_AGENT_SESSION_STATES,
    RECONCILE_OUTCOME_STATE,
    SESSION_STATE_TRANSITIONS,
    AgenticOperatorPort,
    AgentSessionState,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorProbeCommand,
    SessionReconcileOutcome,
    SessionTransitionError,
    validate_session_transition,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
PROBED_MODES = ("exec", "apply", "resume", "workspace-write")


def _probe(
    *, advertised: tuple[str, ...] = PROBED_MODES, exit_code: int = 0
) -> OperatorProbeCommand:
    return OperatorProbeCommand(
        argv=("/usr/local/bin/codex", "--help"),
        exit_code=exit_code,
        stdout_sha256="a" * 64,
        advertised_modes=advertised,
        resolved_version="codex-cli 1.2.3",
    )


def _capabilities(**overrides: object) -> OperatorCapabilitiesV1:
    values: dict[str, object] = {
        "adapter_kind": OperatorAdapterKind.CODEX_CLI,
        "executable": "/usr/local/bin/codex",
        "version": "codex-cli 1.2.3",
        "modes": ("exec", "apply"),
        "supports_resume": True,
        "supports_structured_output": True,
        "supports_non_interactive": True,
        "supports_stop": True,
        "permission_modes": ("workspace-write",),
        "probed_at": NOW,
        "probes": (_probe(),),
    }
    values.update(overrides)
    return OperatorCapabilitiesV1(**values)  # type: ignore[arg-type]


def test_capability_probe_reports_only_probed_modes() -> None:
    capabilities = _capabilities()
    assert set(capabilities.modes) <= set(PROBED_MODES)
    assert capabilities.capability_sha256 == capabilities.expected_capability_sha256()
    assert len(capabilities.capability_sha256) == 64
    assert capabilities.modes == ("exec", "apply")

    with pytest.raises(ValidationError, match="only probed modes"):
        _capabilities(modes=("exec", "invented-mode"))
    with pytest.raises(ValidationError, match="only probed permission modes"):
        _capabilities(permission_modes=("root",))

    # A capability fact needs a successful probe, not merely a recorded one.
    with pytest.raises(ValidationError, match="successful probe"):
        _capabilities(probes=(_probe(exit_code=2),))

    # The hash is deterministic and a supplied different hash is refused.
    replayed = OperatorCapabilitiesV1.model_validate(capabilities.to_payload())
    assert replayed.capability_sha256 == capabilities.capability_sha256
    tampered = capabilities.to_payload()
    tampered["supports_stop"] = False
    tampered["capability_sha256"] = capabilities.capability_sha256
    with pytest.raises(ValidationError, match="does not match the probed facts"):
        OperatorCapabilitiesV1.model_validate(tampered)

    # Duplicate or empty probed modes are refused.
    with pytest.raises(ValidationError, match="must not repeat"):
        OperatorProbeCommand(
            argv=("codex",),
            exit_code=0,
            stdout_sha256="b" * 64,
            advertised_modes=("exec", "exec"),
        )


def test_session_state_machine_matches_the_d7_states() -> None:
    assert {state.value for state in AgentSessionState} == {
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
    }
    assert ACTIVE_AGENT_SESSION_STATES == {
        AgentSessionState.PROVISIONING,
        AgentSessionState.READY,
        AgentSessionState.RUNNING,
        AgentSessionState.AWAITING_INSTRUCTION,
        AgentSessionState.INTERRUPT_REQUESTED,
        AgentSessionState.RECONCILING,
    }
    assert SESSION_STATE_TRANSITIONS[AgentSessionState.CLOSED] == frozenset()
    assert SESSION_STATE_TRANSITIONS[AgentSessionState.FAILED] == frozenset()
    validate_session_transition(AgentSessionState.PROVISIONING, AgentSessionState.READY)
    validate_session_transition(AgentSessionState.RUNNING, AgentSessionState.RECONCILING)
    validate_session_transition(AgentSessionState.LOST, AgentSessionState.READY)
    with pytest.raises(SessionTransitionError):
        validate_session_transition(AgentSessionState.CLOSED, AgentSessionState.RUNNING)
    with pytest.raises(SessionTransitionError):
        validate_session_transition(AgentSessionState.PROVISIONING, AgentSessionState.RUNNING)

    assert set(RECONCILE_OUTCOME_STATE) == set(SessionReconcileOutcome)
    assert RECONCILE_OUTCOME_STATE[SessionReconcileOutcome.TERMINATED_CLEAN] is (
        AgentSessionState.CLOSED
    )
    for outcome in (
        SessionReconcileOutcome.LOST_WITH_HANDOFF,
        SessionReconcileOutcome.LOST_UNRECONCILED,
        SessionReconcileOutcome.RESUME_AVAILABLE,
        SessionReconcileOutcome.RESUME_UNAVAILABLE,
    ):
        assert RECONCILE_OUTCOME_STATE[outcome] is AgentSessionState.LOST
    assert RECONCILE_OUTCOME_STATE[SessionReconcileOutcome.SESSION_ALIVE] is None


def test_port_declares_the_ten_d7_operations() -> None:
    expected = {
        "probe_capabilities",
        "prepare_session",
        "start_session",
        "get_session_status",
        "send_instruction",
        "request_handoff",
        "interrupt_session",
        "resume_session",
        "collect_results",
        "reconcile_session",
    }
    members = set(dir(AgenticOperatorPort))
    assert expected <= members
    assert all(callable(getattr(AgenticOperatorPort, name)) for name in expected)
    assert inspect.iscoroutinefunction(AgenticOperatorPort.probe_capabilities)
    assert "adapter_kind" in members

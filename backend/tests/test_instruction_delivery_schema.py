"""Offline oracles for the F3-D instruction delivery schema (M24.1 ID-1).

Covers the §3.2 state machine and the invariants migration 0027 enforces
(tokens, attempts, exact digests, note diffs, timestamps, cancellation), the
in-memory repository's compare-and-swap contract, and the additive
``supports_next_run_instruction`` capability flag.  PostgreSQL shape and the
upgrade/downgrade oracles live in the integration modules.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_persistence import (
    InMemoryAgentSessionRepository,
    VersionConflictError,
)
from cti_app.domain.agentic_operator import (
    INSTRUCTION_PENDING_STATES,
    INSTRUCTION_STATE_TRANSITIONS,
    TERMINAL_INSTRUCTION_STATES,
    AgentSessionInstructionV1,
    AgentSessionState,
    AgentSessionStateError,
    AgentSessionV1,
    InstructionDeliveryMode,
    InstructionState,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorConfinementV1,
    OperatorProbeCommand,
    SessionTransitionError,
    instruction_text_sha256,
    validate_instruction_transition,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)
TOKEN = UUID("6d1d4a8e-2a5f-4f0c-9a03-6f1f5f3f8f21")
SHA = "b" * 64
NOTE_DIFFS = (
    {
        "path": "notes/humaines/piste.md",
        "queued_revision_id": "rev-a",
        "current_revision_id": "rev-b",
        "diff": "-old line\n+new line",
    },
)


def _instruction(**overrides: object) -> AgentSessionInstructionV1:
    text = str(overrides.pop("text", "Check the second-stage gate."))
    values: dict[str, object] = {
        "investigation_id": uuid4(),
        "task_id": uuid4(),
        "session_id": uuid4(),
        "idempotency_key": "instruction-1",
        "text": text,
        "text_sha256": instruction_text_sha256(text),
        "state": InstructionState.QUEUED,
        "created_by_kind": "human",
        "created_by_id": "analyst:test",
        "queued_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return AgentSessionInstructionV1(**values)  # type: ignore[arg-type]


def _reserved() -> AgentSessionInstructionV1:
    return _instruction().with_delivery_reserved(token=TOKEN, delivery_sha256=SHA, now=LATER)


def _delivered() -> AgentSessionInstructionV1:
    return _reserved().with_delivery_confirmed(
        delivery_mode=InstructionDeliveryMode.NEXT_RUN,
        delivery_sha256=SHA,
        delivery_note_diffs=NOTE_DIFFS,
        now=LATER,
    )


def _active_session(session_id: UUID, *, task_id: UUID) -> AgentSessionV1:
    """The active session row the drain's candidate join needs."""
    capabilities = OperatorCapabilitiesV1(
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        executable="/usr/local/bin/codex",
        version="codex-cli 1.2.3",
        modes=("exec",),
        supports_resume=False,
        supports_structured_output=True,
        supports_non_interactive=True,
        supports_stop=True,
        supports_next_run_instruction=True,
        permission_modes=("workspace-write",),
        confinement=OperatorConfinementV1(
            profile="bwrap-reasoning-session",
            bwrap_ok=True,
            netns=True,
            network_mode="isolated_egress",
            loopback_isolated=True,
        ),
        probed_at=NOW,
        probes=(
            OperatorProbeCommand(
                argv=("/usr/local/bin/codex", "--help"),
                exit_code=0,
                stdout_sha256="a" * 64,
                advertised_modes=("exec", "workspace-write"),
            ),
        ),
    )
    return AgentSessionV1(
        id=session_id,
        investigation_id=uuid4(),
        task_id=task_id,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        state=AgentSessionState.RUNNING,
        state_version=1,
        capability_probe=capabilities.to_payload(),
        capability_sha256=capabilities.capability_sha256,
        probed_at=capabilities.probed_at,
        auth_secret_nonce=b"s" * 32,
        auth_secret_hash=hashlib.sha256(b"s" * 32).hexdigest(),
        auth_secret_issued_at=NOW,
        opened_at=NOW,
        idempotency_key=f"session-{session_id}",
        created_at=NOW,
        updated_at=NOW,
        host_session_ref=f"host-{session_id}",
    )


# --------------------------------------------------------------- state machine


def test_instruction_state_machine_matches_the_delivery_design() -> None:
    assert {state.value for state in InstructionState} == {
        "queued",
        "delivering",
        "delivered",
        "acknowledged",
        "delivery_unknown",
        "cancelled",
    }
    assert INSTRUCTION_PENDING_STATES == {
        InstructionState.QUEUED,
        InstructionState.DELIVERING,
        InstructionState.DELIVERY_UNKNOWN,
    }
    assert TERMINAL_INSTRUCTION_STATES == {
        InstructionState.ACKNOWLEDGED,
        InstructionState.CANCELLED,
    }
    assert INSTRUCTION_STATE_TRANSITIONS[InstructionState.DELIVERED] == frozenset(
        {InstructionState.ACKNOWLEDGED}
    )
    assert INSTRUCTION_STATE_TRANSITIONS[InstructionState.ACKNOWLEDGED] == frozenset()
    assert INSTRUCTION_STATE_TRANSITIONS[InstructionState.CANCELLED] == frozenset()

    allowed = (
        (InstructionState.QUEUED, InstructionState.DELIVERING),
        (InstructionState.QUEUED, InstructionState.CANCELLED),
        (InstructionState.DELIVERING, InstructionState.DELIVERED),
        (InstructionState.DELIVERING, InstructionState.QUEUED),
        (InstructionState.DELIVERING, InstructionState.DELIVERY_UNKNOWN),
        (InstructionState.DELIVERY_UNKNOWN, InstructionState.DELIVERED),
        (InstructionState.DELIVERY_UNKNOWN, InstructionState.QUEUED),
        (InstructionState.DELIVERY_UNKNOWN, InstructionState.CANCELLED),
        (InstructionState.DELIVERED, InstructionState.ACKNOWLEDGED),
    )
    for from_state, to_state in allowed:
        validate_instruction_transition(from_state, to_state)

    forbidden = (
        (InstructionState.QUEUED, InstructionState.DELIVERED),
        (InstructionState.QUEUED, InstructionState.ACKNOWLEDGED),
        (InstructionState.DELIVERED, InstructionState.CANCELLED),
        (InstructionState.DELIVERED, InstructionState.QUEUED),
        (InstructionState.DELIVERY_UNKNOWN, InstructionState.ACKNOWLEDGED),
        (InstructionState.ACKNOWLEDGED, InstructionState.DELIVERED),
        (InstructionState.CANCELLED, InstructionState.QUEUED),
        (InstructionState.CANCELLED, InstructionState.DELIVERY_UNKNOWN),
    )
    for from_state, to_state in forbidden:
        with pytest.raises(SessionTransitionError):
            validate_instruction_transition(from_state, to_state)


def test_queued_is_never_delivered_by_a_state_write() -> None:
    """ID-3: only a proven host effect advances ``queued`` to ``delivered``."""
    with pytest.raises(SessionTransitionError):
        _instruction().with_state(InstructionState.DELIVERED, now=LATER)


# --------------------------------------------------------------- invariants


def test_a_queued_instruction_carries_no_delivery_evidence() -> None:
    with pytest.raises(AgentSessionStateError, match="delivery_token"):
        _instruction(delivery_token=TOKEN)
    with pytest.raises(AgentSessionStateError, match="delivery_sha256"):
        _instruction(delivery_sha256=SHA)
    with pytest.raises(AgentSessionStateError, match="delivery_mode"):
        _instruction(delivery_mode=InstructionDeliveryMode.LIVE)
    with pytest.raises(AgentSessionStateError, match="delivery_note_diffs"):
        _instruction(delivery_note_diffs=NOTE_DIFFS)
    with pytest.raises(AgentSessionStateError, match="delivered_at"):
        _instruction(delivered_at=NOW)
    with pytest.raises(AgentSessionStateError, match="delivery_unknown_at"):
        _instruction(delivery_unknown_at=NOW)
    with pytest.raises(AgentSessionStateError, match="cancelled_at"):
        _instruction(cancelled_at=NOW)
    with pytest.raises(AgentSessionStateError, match="cancel_reason"):
        _instruction(cancel_reason="stop now")
    with pytest.raises(AgentSessionStateError, match="delivery_attempts"):
        _instruction(delivery_attempts=-1)
    with pytest.raises(TypeError, match="delivery_attempts"):
        _instruction(delivery_attempts=True)


def test_proven_and_in_flight_states_require_their_evidence() -> None:
    with pytest.raises(AgentSessionStateError, match="delivered_at"):
        _instruction(state=InstructionState.DELIVERED)
    with pytest.raises(AgentSessionStateError, match="acknowledged_at"):
        _instruction(state=InstructionState.ACKNOWLEDGED, delivered_at=NOW)
    with pytest.raises(AgentSessionStateError, match="delivery_unknown_at"):
        _instruction(state=InstructionState.DELIVERY_UNKNOWN)
    with pytest.raises(AgentSessionStateError, match="delivery_token"):
        _instruction(
            state=InstructionState.DELIVERY_UNKNOWN,
            delivery_unknown_at=NOW,
            delivery_sha256=SHA,
        )
    with pytest.raises(AgentSessionStateError, match="delivery_sha256"):
        _instruction(
            state=InstructionState.DELIVERING,
            delivery_token=TOKEN,
        )
    with pytest.raises(AgentSessionStateError, match="last_delivery_error"):
        _instruction(
            state=InstructionState.DELIVERED,
            delivered_at=NOW,
            last_delivery_error="timed out",
        )
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        _instruction(delivery_sha256="0" * 63)


def test_reservation_records_token_attempt_and_exact_digest() -> None:
    reserved = _reserved()
    assert reserved.state is InstructionState.DELIVERING
    assert reserved.delivery_attempts == 1
    assert reserved.delivery_token == TOKEN
    assert reserved.delivery_sha256 == SHA
    assert reserved.delivery_attempted_at == LATER
    assert reserved.delivery_unknown_at is None
    assert reserved.delivered_at is None
    assert reserved.updated_at == LATER

    unknown = reserved.with_state(
        InstructionState.DELIVERY_UNKNOWN, now=LATER, last_delivery_error="transport timeout"
    )
    # An ambiguous attempt is never re-reserved blind: absence is proved first.
    with pytest.raises(AgentSessionStateError, match="only a queued instruction"):
        unknown.with_delivery_reserved(token=uuid4(), delivery_sha256=SHA, now=LATER)
    requeued = unknown.with_state(
        InstructionState.QUEUED, now=LATER, last_delivery_error="absence proved"
    )
    retry = requeued.with_delivery_reserved(token=uuid4(), delivery_sha256=SHA, now=LATER)
    assert retry.delivery_attempts == 2
    assert retry.delivery_unknown_at is None
    assert retry.last_delivery_error is None

    with pytest.raises(AgentSessionStateError, match="only a queued instruction"):
        reserved.with_delivery_reserved(token=uuid4(), delivery_sha256=SHA, now=LATER)


def test_delivery_confirmation_records_mode_digest_and_note_diffs() -> None:
    delivered = _delivered()
    assert delivered.state is InstructionState.DELIVERED
    assert delivered.delivered_at == LATER
    assert delivered.delivery_mode is InstructionDeliveryMode.NEXT_RUN
    assert delivered.delivery_sha256 == SHA
    assert delivered.delivery_note_diffs == NOTE_DIFFS
    assert delivered.delivery_token == TOKEN
    assert delivered.last_delivery_error is None
    payload = delivered.to_payload()
    assert payload["delivery_mode"] == "next_run"
    assert payload["delivery_sha256"] == SHA
    assert payload["delivery_note_diffs"] == [dict(item) for item in NOTE_DIFFS]
    assert payload["delivery_attempts"] == 1

    acknowledged = delivered.with_state(InstructionState.ACKNOWLEDGED, now=LATER)
    assert acknowledged.acknowledged_at == LATER
    assert acknowledged.delivered_at == LATER
    assert acknowledged.delivery_mode is InstructionDeliveryMode.NEXT_RUN

    with pytest.raises(AgentSessionStateError, match="in-flight"):
        _instruction().with_delivery_confirmed(
            delivery_mode=InstructionDeliveryMode.LIVE,
            delivery_sha256=SHA,
            delivery_note_diffs=(),
            now=LATER,
        )


def test_refusal_and_ambiguous_receipt_are_distinct_transitions() -> None:
    reserved = _reserved()
    refused = reserved.with_state(
        InstructionState.QUEUED, now=LATER, last_delivery_error="host refused: busy"
    )
    assert refused.state is InstructionState.QUEUED
    assert refused.delivery_token is None
    assert refused.delivery_sha256 is None
    assert refused.delivery_attempts == 1
    assert refused.last_delivery_error == "host refused: busy"

    unknown = reserved.with_state(
        InstructionState.DELIVERY_UNKNOWN, now=LATER, last_delivery_error="timeout after send"
    )
    assert unknown.delivery_token == TOKEN
    assert unknown.delivery_sha256 == SHA
    assert unknown.delivery_unknown_at == LATER
    assert unknown.last_delivery_error == "timeout after send"

    # A host event proves the effect: no second send, no invented evidence.
    proven = unknown.with_state(InstructionState.DELIVERED, now=LATER)
    assert proven.state is InstructionState.DELIVERED
    assert proven.delivery_token == TOKEN
    assert proven.delivery_sha256 == SHA
    assert proven.delivery_unknown_at is None
    assert proven.last_delivery_error is None

    # Absence of delivery is proven: the row returns to the queue with no token.
    absent = unknown.with_state(
        InstructionState.QUEUED, now=LATER, last_delivery_error="absence proved"
    )
    assert absent.delivery_token is None
    assert absent.delivery_sha256 is None
    assert absent.delivery_unknown_at is None
    assert absent.delivery_attempts == 1


def test_cancellation_is_motivated_and_terminal() -> None:
    cancelled = _instruction().with_cancellation(reason="handed_off:handoff-1", now=LATER)
    assert cancelled.state is InstructionState.CANCELLED
    assert cancelled.cancelled_at == LATER
    assert cancelled.cancel_reason == "handed_off:handoff-1"
    assert cancelled.delivery_token is None
    with pytest.raises(SessionTransitionError):
        cancelled.with_state(InstructionState.ACKNOWLEDGED, now=LATER)

    with pytest.raises(AgentSessionStateError, match="cancel_reason"):
        _instruction().with_state(InstructionState.CANCELLED, now=LATER)
    with pytest.raises(AgentSessionStateError, match="never-delivered"):
        _delivered().with_cancellation(reason="too late", now=LATER)
    with pytest.raises(AgentSessionStateError, match="never-delivered"):
        cancelled.with_cancellation(reason="again", now=LATER)

    unknown = _reserved().with_state(InstructionState.DELIVERY_UNKNOWN, now=LATER)
    cancelled_unknown = unknown.with_cancellation(reason="human decision", now=LATER)
    assert cancelled_unknown.delivery_token is None
    assert cancelled_unknown.delivery_sha256 is None
    assert cancelled_unknown.delivery_unknown_at is None


# --------------------------------------------------------------- fake repository


async def test_fake_repository_reserves_one_attempt_with_its_token() -> None:
    repository = InMemoryAgentSessionRepository()
    queued = _instruction()
    stored, created = await repository.add_instruction(queued)
    assert created is True
    assert stored.state is InstructionState.QUEUED

    reserved = queued.with_delivery_reserved(token=TOKEN, delivery_sha256=SHA, now=LATER)
    stored = await repository.update_instruction_delivery(
        reserved, expected_state=InstructionState.QUEUED, expected_delivery_token=None
    )
    assert stored is reserved
    assert (
        await repository.get_instruction_by_token(
            queued.id, TOKEN, state=InstructionState.DELIVERING
        )
        == reserved
    )
    assert await repository.get_instruction_by_token(queued.id, uuid4()) is None
    assert (
        await repository.get_instruction_by_token(
            queued.id, TOKEN, state=InstructionState.DELIVERED
        )
        is None
    )

    with pytest.raises(VersionConflictError):
        await repository.update_instruction_delivery(
            reserved, expected_state=InstructionState.QUEUED, expected_delivery_token=None
        )


async def test_fake_repository_cas_rejects_a_stale_state_or_token() -> None:
    repository = InMemoryAgentSessionRepository()
    queued = _instruction()
    await repository.add_instruction(queued)
    reserved = queued.with_delivery_reserved(token=TOKEN, delivery_sha256=SHA, now=LATER)
    await repository.update_instruction_delivery(
        reserved, expected_state=InstructionState.QUEUED, expected_delivery_token=None
    )
    delivered = reserved.with_delivery_confirmed(
        delivery_mode=InstructionDeliveryMode.NEXT_RUN,
        delivery_sha256=SHA,
        delivery_note_diffs=NOTE_DIFFS,
        now=LATER,
    )
    with pytest.raises(VersionConflictError):
        await repository.update_instruction_delivery(
            delivered,
            expected_state=InstructionState.DELIVERING,
            expected_delivery_token=uuid4(),
        )
    with pytest.raises(VersionConflictError):
        await repository.update_instruction_delivery(
            delivered,
            expected_state=InstructionState.QUEUED,
            expected_delivery_token=TOKEN,
        )
    assert (
        await repository.update_instruction_delivery(
            delivered,
            expected_state=InstructionState.DELIVERING,
            expected_delivery_token=TOKEN,
        )
        is delivered
    )


async def test_fake_repository_lists_only_pending_instructions_in_queue_order() -> None:
    repository = InMemoryAgentSessionRepository()
    first = _instruction(idempotency_key="first", queued_at=NOW)
    second = _instruction(idempotency_key="second", queued_at=NOW + timedelta(seconds=1))
    ambiguous = (
        _instruction(idempotency_key="ambiguous", queued_at=NOW - timedelta(seconds=5))
        .with_delivery_reserved(token=TOKEN, delivery_sha256=SHA, now=NOW)
        .with_state(
            InstructionState.DELIVERY_UNKNOWN, now=LATER, last_delivery_error="delivery_timeout"
        )
    )
    delivered = _delivered()
    acknowledged = _delivered().with_state(InstructionState.ACKNOWLEDGED, now=LATER)
    cancelled = _instruction(idempotency_key="cancelled").with_cancellation(
        reason="handed_off:h1", now=LATER
    )
    for instruction in (second, delivered, first, acknowledged, cancelled, ambiguous):
        await repository.add(
            _active_session(instruction.session_id, task_id=instruction.task_id)
        )
        _, created = await repository.add_instruction(instruction)
        assert created is True
    pending = await repository.list_pending_instructions(limit=10)
    assert [item.id for item in pending] == [ambiguous.id, first.id, second.id]
    assert (await repository.list_pending_instructions(limit=1))[0].id == ambiguous.id
    # The delivery batch only wants rows it can actually deliver (R4-F2).
    deliverable = await repository.list_pending_instructions(
        limit=10, states=(InstructionState.QUEUED,)
    )
    assert [item.id for item in deliverable] == [first.id, second.id]
    with pytest.raises(ValueError):
        await repository.list_pending_instructions(limit=0)


async def test_fake_repository_cancels_with_its_reason_and_refuses_terminal_rows() -> None:
    repository = InMemoryAgentSessionRepository()
    queued = _instruction()
    await repository.add_instruction(queued)
    cancelled = await repository.cancel_instruction(
        queued.id,
        expected_state=InstructionState.QUEUED,
        expected_delivery_token=None,
        reason="handed_off:handoff-9",
        now=LATER,
    )
    assert cancelled.state is InstructionState.CANCELLED
    assert cancelled.cancel_reason == "handed_off:handoff-9"
    with pytest.raises(VersionConflictError):
        await repository.cancel_instruction(
            queued.id,
            expected_state=InstructionState.QUEUED,
            expected_delivery_token=None,
            reason="again",
            now=LATER,
        )

    reserved = _instruction(idempotency_key="in-flight").with_delivery_reserved(
        token=TOKEN, delivery_sha256=SHA, now=LATER
    )
    await repository.add_instruction(reserved)
    with pytest.raises(AgentSessionStateError, match="never-delivered"):
        await repository.cancel_instruction(
            reserved.id,
            expected_state=InstructionState.DELIVERING,
            expected_delivery_token=TOKEN,
            reason="too late",
            now=LATER,
        )


# --------------------------------------------------------------- capability flag


def _probe() -> OperatorProbeCommand:
    return OperatorProbeCommand(
        argv=("/usr/local/bin/codex", "--help"),
        exit_code=0,
        stdout_sha256="a" * 64,
        advertised_modes=("exec",),
    )


def _capabilities(**overrides: object) -> OperatorCapabilitiesV1:
    values: dict[str, object] = {
        "adapter_kind": OperatorAdapterKind.CODEX_CLI,
        "executable": "/usr/local/bin/codex",
        "version": "codex-cli 1.2.3",
        "modes": ("exec",),
        "probed_at": NOW,
        "probes": (_probe(),),
    }
    values.update(overrides)
    return OperatorCapabilitiesV1(**values)  # type: ignore[arg-type]


def test_next_run_instruction_flag_is_additive_and_keeps_legacy_hashes() -> None:
    legacy = _capabilities()
    assert legacy.supports_next_run_instruction is False
    assert "supports_next_run_instruction" not in legacy.payload()
    legacy_payload = legacy.to_payload()
    del legacy_payload["supports_next_run_instruction"]
    replayed = OperatorCapabilitiesV1.model_validate(legacy_payload)
    assert replayed.capability_sha256 == legacy.capability_sha256

    advertised = _capabilities(supports_next_run_instruction=True)
    assert advertised.payload()["supports_next_run_instruction"] is True
    assert advertised.capability_sha256 != legacy.capability_sha256
    assert replayed.supports_next_run_instruction is False

    with pytest.raises(ValueError, match="successful probe"):
        _capabilities(
            supports_next_run_instruction=True,
            probes=(_probe().model_copy(update={"exit_code": 2}),),
        )

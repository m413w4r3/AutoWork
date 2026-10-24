from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application import bridge_investigator as bridge_module
from cti_app.application.bridge_investigator import (
    BridgeSubmissionPacer,
    ModelConversationTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
    make_investigation_turn_idempotency_key,
    submit_bridge_consultation_once,
)
from cti_app.application.investigation_advisor_tools import InvestigationAdvisorTool
from cti_app.application.model_gateway import AdapterResult, AdapterResultStatus, ConversationResult
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.model_conversations import (
    ConversationStatus,
    ConversationTurnStatus,
)
from cti_app.domain.model_runs import BridgeSubmissionState, ModelRunStatus, ModelUsage
from cti_app.integrations.models import _retry_after_seconds
from tests.test_model_conversations import _build_service, _ScriptedBridgeAdapter


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class _FakeTransport:
    def __init__(self, outcomes: list[Any], clock: _FakeClock) -> None:
        self.outcomes = deque(outcomes)
        self.clock = clock
        self.calls: list[tuple[int, float | None]] = []
        self.keys: list[str] = []
        self.accepted_successes = 0

    async def submit(self, *, timeout_seconds: float | None = None, **kwargs):
        return await self._submit(0, timeout_seconds, kwargs)

    async def submit_with_attempt(
        self, *, attempt: int, timeout_seconds: float | None = None, **kwargs
    ):
        return await self._submit(attempt, timeout_seconds, kwargs)

    async def _submit(self, attempt: int, timeout_seconds: float | None, arguments):
        self.calls.append((attempt, timeout_seconds))
        self.keys.append(
            make_investigation_turn_idempotency_key(
                investigation_id=arguments["investigation_id"],
                turn_index=arguments["turn_index"],
                context_sha256=arguments["context_sha256"],
                attempt=attempt,
            )
        )
        outcome = self.outcomes.popleft()
        if callable(outcome):
            outcome = await outcome()
        if outcome.status is TurnSubmissionStatus.SUCCEEDED:
            self.accepted_successes += 1
        return outcome


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "openai_bridge_min_submission_interval_seconds": 1,
        "openai_bridge_submission_jitter_fraction": 0,
        "openai_bridge_pre_submission_retry_base_seconds": 2,
        "openai_bridge_pre_submission_retry_max_seconds": 8,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _pacer(clock: _FakeClock, *, interval: float = 1) -> BridgeSubmissionPacer:
    return BridgeSubmissionPacer(
        minimum_interval_seconds=interval,
        jitter_fraction=0,
        clock=clock,
        sleeper=clock.sleep,
        rng=lambda: 0,
    )


async def _submit(
    monkeypatch,
    clock: _FakeClock,
    transport: _FakeTransport,
    *,
    deadline: float,
    minimum_timeout: float = 0,
    retry_base: float = 2,
    retry_max: float = 8,
    pacer: BridgeSubmissionPacer | None = None,
) -> TurnSubmission:
    pacer = pacer or _pacer(clock)
    monkeypatch.setattr(bridge_module, "_shared_bridge_pacer", lambda **_kwargs: pacer)
    return await submit_bridge_consultation_once(
        transport,
        investigation_id=uuid4(),
        subject_id=uuid4(),
        turn_index=0,
        context_sha256="a" * 64,
        prompt="Synthetic temporal recovery test.",
        external_llm_allowed=True,
        settings=_settings(
            openai_bridge_pre_submission_retry_base_seconds=retry_base,
            openai_bridge_pre_submission_retry_max_seconds=retry_max,
        ),
        timeout_seconds=deadline,
        deadline_monotonic=deadline,
        minimum_timeout_seconds=minimum_timeout,
        clock=clock,
    )


def _advisor_result(submission: TurnSubmission):
    tool = object.__new__(InvestigationAdvisorTool)
    tool._settings = SimpleNamespace()
    return tool._submission_result(
        submission,
        "Synthetic prompt",
        (),
        (),
        0,
        "pivot_strategy",
        1,
        {},
        {},
        prompt_data={},
    )


@pytest.mark.asyncio
async def test_pacing_that_exhausts_the_g2_window_refuses_before_dispatch(monkeypatch) -> None:
    clock = _FakeClock()
    pacer = _pacer(clock)
    await pacer.defer_submissions(5)
    monkeypatch.setattr(bridge_module, "_shared_bridge_pacer", lambda **_kwargs: pacer)
    transport = _FakeTransport([], clock)

    result = await submit_bridge_consultation_once(
        transport,
        investigation_id=uuid4(),
        subject_id=uuid4(),
        turn_index=0,
        context_sha256="b" * 64,
        prompt="Pacing must fit inside the approved window.",
        external_llm_allowed=True,
        settings=_settings(),
        timeout_seconds=5,
        deadline_monotonic=5,
        minimum_timeout_seconds=1,
        clock=clock,
    )

    assert result.status is TurnSubmissionStatus.UNAVAILABLE
    assert result.error_code == "bridge_execution_budget_exhausted"
    assert result.submission_state == "pre_submission"
    assert transport.calls == []
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_one_proven_pre_submission_429_retry_uses_retry_after_and_distinct_attempt_key(
    monkeypatch,
) -> None:
    clock = _FakeClock()

    async def rate_limited():
        clock.advance(1)
        return TurnSubmission(
            TurnSubmissionStatus.UNAVAILABLE,
            error_code="bridge_rate_limited",
            submission_state="pre_submission",
            retryable=True,
            retry_after_seconds=_retry_after_seconds("2"),
        )

    async def success():
        clock.advance(1)
        return TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="one answer")

    transport = _FakeTransport([rate_limited, success], clock)
    result = await _submit(monkeypatch, clock, transport, deadline=20, minimum_timeout=2)

    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert transport.calls == [(0, 20), (1, 17)]
    assert transport.keys[0] != transport.keys[1]
    assert [attempt for attempt, _timeout in transport.calls] == [0, 1]
    assert transport.accepted_successes == 1
    assert clock.sleeps == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submission_state",
    [
        None,
        BridgeSubmissionState.SUBMISSION_ATTEMPTED,
        BridgeSubmissionState.POST_SUBMISSION,
        BridgeSubmissionState.EXTERNAL_STATE_UNKNOWN,
        "unexpected_submission_state",
    ],
)
async def test_429_without_pre_submission_proof_never_retries(
    monkeypatch, submission_state
) -> None:
    clock = _FakeClock()
    transport = _FakeTransport(
        [
            TurnSubmission(
                TurnSubmissionStatus.UNAVAILABLE,
                error_code="bridge_rate_limited",
                submission_state=submission_state,
                retryable=True,
                retry_after_seconds=1,
            )
        ],
        clock,
    )

    result = await _submit(monkeypatch, clock, transport, deadline=20, minimum_timeout=1)

    assert result.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert result.retryable is False
    expected_state = (
        BridgeSubmissionState.EXTERNAL_STATE_UNKNOWN
        if submission_state is None or submission_state == "unexpected_submission_state"
        else BridgeSubmissionState(submission_state)
    )
    assert result.submission_state is expected_state
    assert transport.calls == [(0, 20)]
    assert transport.accepted_successes == 0


@pytest.mark.asyncio
async def test_retry_after_beyond_remaining_budget_stays_rate_limited_without_sleep_or_retry(
    monkeypatch,
) -> None:
    clock = _FakeClock()

    async def rate_limited():
        clock.advance(1)
        return TurnSubmission(
            TurnSubmissionStatus.UNAVAILABLE,
            error_code="bridge_rate_limited",
            submission_state="pre_submission",
            retryable=True,
            retry_after_seconds=3,
        )

    transport = _FakeTransport([rate_limited], clock)
    pacer = _pacer(clock)
    result = await _submit(
        monkeypatch,
        clock,
        transport,
        deadline=5,
        minimum_timeout=2,
        pacer=pacer,
    )
    advisor_result = _advisor_result(result)

    assert result.status is TurnSubmissionStatus.UNAVAILABLE
    assert result.error_code == "bridge_rate_limited"
    assert advisor_result.status is ToolInvocationStatus.UNAVAILABLE
    assert advisor_result.output["advisor_status"] == "rate_limited"
    assert advisor_result.output["retry"] is False
    assert transport.calls == [(0, 5)]
    assert transport.accepted_successes == 0
    assert clock.sleeps == []
    assert pacer._blocked_until == 0


@pytest.mark.asyncio
async def test_large_finite_retry_after_is_capped_for_shared_pacer_without_retry(
    monkeypatch,
) -> None:
    clock = _FakeClock()
    transport = _FakeTransport(
        [
            TurnSubmission(
                TurnSubmissionStatus.UNAVAILABLE,
                error_code="bridge_rate_limited",
                submission_state="pre_submission",
                retryable=True,
                retry_after_seconds=10_000,
            )
        ],
        clock,
    )
    pacer = _pacer(clock)

    result = await _submit(
        monkeypatch,
        clock,
        transport,
        deadline=20,
        minimum_timeout=1,
        retry_max=8,
        pacer=pacer,
    )

    assert result.status is TurnSubmissionStatus.UNAVAILABLE
    assert result.error_code == "bridge_rate_limited"
    assert transport.calls == [(0, 20)]
    assert transport.accepted_successes == 0
    assert clock.sleeps == []
    assert pacer._blocked_until == 8


@pytest.mark.asyncio
async def test_429_without_distinct_attempt_support_stays_rate_limited(monkeypatch) -> None:
    clock = _FakeClock()

    class SingleAttemptTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def submit(self, *, timeout_seconds=None, **_kwargs):
            self.calls += 1
            return TurnSubmission(
                TurnSubmissionStatus.UNAVAILABLE,
                error_code="bridge_rate_limited",
                submission_state="pre_submission",
                retryable=True,
                retry_after_seconds=1,
            )

    transport = SingleAttemptTransport()
    result = await _submit(monkeypatch, clock, transport, deadline=20, minimum_timeout=1)
    advisor_result = _advisor_result(result)

    assert result.error_code == "bridge_rate_limited"
    assert advisor_result.output["advisor_status"] == "rate_limited"
    assert transport.calls == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_after",
    [
        -3,
        0,
        float("nan"),
        float("inf"),
        _retry_after_seconds("not a date"),
        _retry_after_seconds("-3"),
    ],
)
async def test_invalid_or_negative_retry_after_uses_bounded_fallback(
    monkeypatch, retry_after: Any
) -> None:
    clock = _FakeClock()

    async def rate_limited():
        return TurnSubmission(
            TurnSubmissionStatus.UNAVAILABLE,
            error_code="bridge_rate_limited",
            submission_state="pre_submission",
            retryable=True,
            retry_after_seconds=retry_after,
        )

    transport = _FakeTransport(
        [rate_limited, lambda: asyncio.sleep(0, result=TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED, output_text="one answer"
        ))],
        clock,
    )
    result = await _submit(monkeypatch, clock, transport, deadline=20, minimum_timeout=1)

    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert transport.calls == [(0, 20), (1, 18)]
    assert transport.accepted_successes == 1
    assert clock.sleeps == [2]


@pytest.mark.asyncio
async def test_late_transport_success_is_rejected_and_never_parsed(monkeypatch) -> None:
    clock = _FakeClock()

    async def late_success():
        clock.advance(5)
        return TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="late answer")

    transport = _FakeTransport([late_success], clock)
    result = await _submit(monkeypatch, clock, transport, deadline=5)
    advisor_result = _advisor_result(result)

    assert result.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert result.output_text is None
    assert advisor_result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert "blocks" not in advisor_result.output
    assert transport.calls == [(0, 5)]
    assert transport.accepted_successes == 1  # observed externally, rejected locally


@pytest.mark.asyncio
async def test_transport_timeout_is_ambiguous_and_never_retried(monkeypatch) -> None:
    clock = _FakeClock()

    async def timeout_after_dispatch():
        clock.advance(1)
        raise TimeoutError

    transport = _FakeTransport([timeout_after_dispatch], clock)
    result = await _submit(monkeypatch, clock, transport, deadline=10)

    assert result.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert result.submission_state == "external_state_unknown"
    assert result.retryable is False
    assert transport.calls == [(0, 10)]
    assert transport.accepted_successes == 0


@pytest.mark.asyncio
async def test_unknown_provider_status_is_reviewable_and_never_retried(monkeypatch) -> None:
    clock = _FakeClock()
    transport = _FakeTransport(
        [
            TurnSubmission(
                TurnSubmissionStatus.NEEDS_REVIEW,
                output_text="untrusted possible answer",
                error_code="bridge_status_unknown",
                submission_state="external_state_unknown",
            )
        ],
        clock,
    )

    result = await _submit(monkeypatch, clock, transport, deadline=10)
    advisor_result = _advisor_result(result)

    assert result.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert advisor_result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert "blocks" not in advisor_result.output
    assert transport.calls == [(0, 10)]
    assert transport.accepted_successes == 0


@pytest.mark.asyncio
async def test_late_adapter_reply_is_durably_rejected_as_needs_review(tmp_path) -> None:
    class LateAdapter(_ScriptedBridgeAdapter):
        async def invoke(self, request, *, role, output_schema=None):
            del role, output_schema
            self.calls.append(request)
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                # Simulate an adapter that returns after the timeout cancellation.
                await asyncio.sleep(0.01)
            context = request.conversation
            assert context is not None
            return AdapterResult(
                status=AdapterResultStatus.COMPLETED,
                provider=self.provider,
                requested_model=self.requested_model,
                actual_model_version=self.requested_model,
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                output_text="late answer must not be committed",
                conversation=ConversationResult(
                    id=str(context.id),
                    mode=context.mode,
                    external_locator=None,
                    turn_id="late-turn",
                    verified=True,
                ),
            )

    adapter = LateAdapter("unused")
    subject_id = uuid4()
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    result = await ModelConversationTurnTransport(service).submit(
        investigation_id=uuid4(),
        subject_id=subject_id,
        turn_index=0,
        context_sha256="d" * 64,
        prompt="Synthetic late-response test.",
        external_llm_allowed=True,
        timeout_seconds=0.05,
    )

    turn = next(iter(state.turns.values()))
    run = state.model_runs[turn.model_run_id]
    assert result.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert result.output_text is None
    assert turn.status is ConversationTurnStatus.NEEDS_REVIEW
    assert run.status is ModelRunStatus.NEEDS_REVIEW
    assert run.submission_state.value == "external_state_unknown"
    assert len(adapter.calls) == 1
    assert not run.output_references


@pytest.mark.asyncio
async def test_unknown_adapter_outcome_is_durably_reviewable_without_retry(tmp_path) -> None:
    class UnknownOutcomeAdapter(_ScriptedBridgeAdapter):
        async def invoke(self, request, *, role, output_schema=None):
            del role, output_schema
            self.calls.append(request)
            return AdapterResult(
                status=AdapterResultStatus.NEEDS_REVIEW,
                provider=self.provider,
                requested_model=self.requested_model,
                actual_model_version=self.requested_model,
                usage=ModelUsage(input_tokens=1, output_tokens=0, total_tokens=1),
                metadata={"reason": "synthetic_provider_status_unknown", "output_chars": 0},
            )

    adapter = UnknownOutcomeAdapter("unused")
    subject_id = uuid4()
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    result = await ModelConversationTurnTransport(service).submit(
        investigation_id=uuid4(),
        subject_id=subject_id,
        turn_index=0,
        context_sha256="e" * 64,
        prompt="Synthetic unknown provider outcome.",
        external_llm_allowed=True,
    )

    turn = next(iter(state.turns.values()))
    run = state.model_runs[turn.model_run_id]
    assert result.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert turn.status is ConversationTurnStatus.NEEDS_REVIEW
    assert run.status is ModelRunStatus.NEEDS_REVIEW
    assert len(adapter.calls) == 1
    assert result.output_text is None


@pytest.mark.asyncio
async def test_crash_replay_seals_durable_inflight_submission_without_second_dispatch(
    tmp_path, monkeypatch
) -> None:
    entered = asyncio.Event()

    class BlockingAdapter(_ScriptedBridgeAdapter):
        async def invoke(self, request, *, role, output_schema=None):
            del role, output_schema
            self.calls.append(request)
            entered.set()
            await asyncio.Event().wait()

    adapter = BlockingAdapter("unused")
    subject_id = uuid4()
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    transport = ModelConversationTurnTransport(service)
    investigation_id = uuid4()
    args = {
        "investigation_id": investigation_id,
        "subject_id": subject_id,
        "turn_index": 0,
        "context_sha256": "c" * 64,
        "prompt": "Durable synthetic crash-recovery prompt.",
        "external_llm_allowed": True,
    }
    task = asyncio.create_task(transport.submit(**args))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    turn = next(iter(state.turns.values()))
    run = state.model_runs[turn.model_run_id]
    conversation = state.conversations[turn.conversation_id]
    # Simulate process death by restoring the committed pre-finalization rows.
    turn.status = ConversationTurnStatus.RUNNING
    turn.error_code = None
    turn.error_message = None
    turn.error_details = None
    turn.finished_at = None
    run.status = ModelRunStatus.RUNNING
    run.error_code = None
    run.error_message = None
    run.error_details = None
    run.finished_at = None
    conversation.status = ConversationStatus.BUSY

    recovered = await transport.submit(**args)

    assert recovered.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert state.turns[turn.id].status is ConversationTurnStatus.NEEDS_REVIEW
    assert state.model_runs[run.id].status is ModelRunStatus.NEEDS_REVIEW
    assert len(adapter.calls) == 1
    assert recovered.output_text is None


@pytest.mark.asyncio
async def test_crash_after_durable_not_submitted_state_replays_as_review_without_dispatch(
    tmp_path, monkeypatch
) -> None:
    adapter = _ScriptedBridgeAdapter("unused")
    subject_id = uuid4()
    service, state = _build_service(adapter, tmp_path, known_subject_ids={subject_id})
    transport = ModelConversationTurnTransport(service)
    investigation_id = uuid4()
    args = {
        "investigation_id": investigation_id,
        "subject_id": subject_id,
        "turn_index": 0,
        "context_sha256": "f" * 64,
        "prompt": "Synthetic pre-dispatch crash-recovery prompt.",
        "external_llm_allowed": True,
    }

    async def simulate_process_death_before_gateway_claim(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        service._gateway,
        "execute",
        simulate_process_death_before_gateway_claim,
    )
    with pytest.raises(asyncio.CancelledError):
        await transport.submit(**args)

    turn = next(iter(state.turns.values()))
    run = state.model_runs[turn.model_run_id]
    conversation = state.conversations[turn.conversation_id]
    assert run.submission_state.value == "not_submitted"
    # Restore the committed rows as a process crash would leave them.
    turn.status = ConversationTurnStatus.RUNNING
    turn.error_code = None
    turn.error_message = None
    turn.error_details = None
    turn.finished_at = None
    run.status = ModelRunStatus.RUNNING
    run.error_code = None
    run.error_message = None
    run.error_details = None
    run.finished_at = None
    conversation.status = ConversationStatus.BUSY

    recovered = await transport.submit(**args)

    assert recovered.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert state.turns[turn.id].status is ConversationTurnStatus.NEEDS_REVIEW
    assert state.model_runs[run.id].status is ModelRunStatus.NEEDS_REVIEW
    assert len(adapter.calls) == 0
    assert recovered.output_text is None

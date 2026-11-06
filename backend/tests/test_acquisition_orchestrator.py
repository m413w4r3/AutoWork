from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime

import pytest

from cti_app.application.acquisition_fallbacks import AcquisitionProbe, AcquisitionRequest
from cti_app.application.acquisition_orchestrator import (
    ArticleAcquisitionOrchestrator,
    DomainConcurrencyLimiter,
)
from cti_app.application.http_collection import CollectedResponse
from cti_app.application.jobs import JobCancelledError
from cti_app.domain.acquisition import (
    AcquisitionCapabilityState,
    AcquisitionObservationKind,
    AcquisitionOutcome,
    AcquisitionPolicyDecision,
    EvidenceClass,
    SourceEquivalenceClass,
)
from cti_app.domain.collection import DetectedMimeType, SourceAcquisitionMethod

StrategyHandler = Callable[[AcquisitionRequest], Awaitable[Sequence[AcquisitionProbe]]]


class FakeStrategy:
    def __init__(
        self,
        name: str,
        handler: StrategyHandler,
        *,
        enabled: bool = True,
        timeout_seconds: float = 1.0,
        capability_state: AcquisitionCapabilityState | None = None,
    ) -> None:
        self.name = name
        self.version = "test"
        self.method = SourceAcquisitionMethod.DIRECT_HTTP
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        if capability_state is not None:
            self.capability_state = capability_state
        self._handler = handler
        self.calls = 0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        self.calls += 1
        return await self._handler(request)


def _response(url: str = "https://example.com/article") -> CollectedResponse:
    body = b"article text"
    digest = hashlib.sha256(body).hexdigest()
    return CollectedResponse(
        requested_url=url,
        final_url=url,
        redirect_chain=(),
        status=200,
        headers={"content-type": "text/plain"},
        declared_content_type="text/plain",
        detected_content_type=DetectedMimeType.TEXT,
        encoded_body=body,
        decoded_body=body,
        encoded_size=len(body),
        encoded_sha256=digest,
        decoded_size=len(body),
        decoded_sha256=digest,
        content_encoding="identity",
        acquired_at=datetime.now(UTC),
    )


def _probe(
    outcome: AcquisitionOutcome | str,
    *,
    response: CollectedResponse | None = None,
    reason_code: str | None = None,
    policy_decision: AcquisitionPolicyDecision = AcquisitionPolicyDecision.ALLOWED,
    equivalence_class: SourceEquivalenceClass | None = None,
    evidence_class: EvidenceClass | None = None,
) -> AcquisitionProbe:
    return AcquisitionProbe(
        strategy_name="fake",
        strategy_version="test",
        method=SourceAcquisitionMethod.DIRECT_HTTP,
        requested_url="https://example.com/article",
        outcome=outcome,  # type: ignore[arg-type]
        reason_code=reason_code,
        response=response,
        http_status=response.status if response else None,
        encoded_size=response.encoded_size if response else None,
        encoded_sha256=response.encoded_sha256 if response else None,
        duration_ms=1,
        policy_decision=policy_decision,
        equivalence_class=equivalence_class,
        evidence_class=evidence_class,
    )


def _request(
    fetch: Callable[[str, dict[str, str], float, bool], Awaitable[CollectedResponse]] | None = None,
) -> AcquisitionRequest:
    async def default_fetch(
        url: str, headers: dict[str, str], timeout_seconds: float, allow_binary: bool
    ) -> CollectedResponse:
        del headers, timeout_seconds, allow_binary
        return _response(url)

    return AcquisitionRequest(
        requested_url="https://example.com/article",
        title="Example article",
        published_at=None,
        response_body=None,
        response_status=None,
        response_content_type=None,
        response_headers={},
        link_header=None,
        fetch=fetch or default_fetch,
    )


def _handler(*probes: AcquisitionProbe) -> StrategyHandler:
    async def acquire(_request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        return probes

    return acquire


def test_ordered_strategies_stop_on_first_accepted_success_and_keep_probes() -> None:
    async def run() -> None:
        call_order: list[str] = []

        async def run_first(_request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            call_order.append("first")
            return (_probe(AcquisitionOutcome.FAILED),)

        first = FakeStrategy("first", run_first)
        winner_response = _response()

        async def run_second(_request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            call_order.append("second")
            return (
                _probe(AcquisitionOutcome.REJECTED, reason_code="unrelated_page"),
                _probe(AcquisitionOutcome.SUCCEEDED, response=winner_response),
                _probe(AcquisitionOutcome.FAILED),
            )

        second = FakeStrategy(
            "second",
            run_second,
        )

        async def run_third(_request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            call_order.append("third")
            return (_probe(AcquisitionOutcome.SUCCEEDED),)

        third = FakeStrategy("third", run_third)
        result = await ArticleAcquisitionOrchestrator(
            (first, second, third),
            deadline_seconds=1.0,
            max_attempts=10,
            per_domain_concurrency=2,
        ).run(_request())

        assert call_order == ["first", "second"]
        assert [probe.reason_code for probe in result.probes] == [
            None,
            "unrelated_page",
            None,
            None,
        ]
        assert result.outcome is AcquisitionOutcome.SUCCEEDED
        assert result.winner is result.probes[2]
        assert result.winner.response is winner_response
        assert first.calls == 1
        assert second.calls == 1
        assert third.calls == 0
        assert result.attempts == 4

    asyncio.run(run())


def test_all_fail_returns_all_probes_without_a_winner() -> None:
    async def run() -> None:
        first_probe = _probe(AcquisitionOutcome.FAILED, reason_code="timeout")
        second_probe = _probe(AcquisitionOutcome.REJECTED, reason_code="unrelated_page")
        result = await ArticleAcquisitionOrchestrator(
            (
                FakeStrategy("first", _handler(first_probe)),
                FakeStrategy("second", _handler(second_probe)),
            ),
            deadline_seconds=1.0,
            max_attempts=5,
            per_domain_concurrency=1,
        ).run(_request())

        assert result.outcome is AcquisitionOutcome.FAILED
        assert result.winner is None
        assert result.probes == (first_probe, second_probe)
        assert result.attempts == 2
        assert result.reason_code == "unrelated_page"

    asyncio.run(run())


def test_disabled_strategy_is_recorded_without_calling_acquire() -> None:
    async def run() -> None:
        disabled = FakeStrategy(
            "disabled",
            _handler(_probe(AcquisitionOutcome.SUCCEEDED, response=_response())),
            enabled=False,
        )
        result = await ArticleAcquisitionOrchestrator(
            (disabled,),
            deadline_seconds=1.0,
            max_attempts=1,
            per_domain_concurrency=1,
        ).run(_request())

        assert disabled.calls == 0
        assert len(result.probes) == 1
        assert result.probes[0].outcome is AcquisitionOutcome.SKIPPED
        assert result.probes[0].reason_code == "disabled_by_configuration"
        assert (
            result.probes[0].capability_state
            is AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )
        assert result.attempts == 0

    asyncio.run(run())


def test_per_strategy_timeout_is_recorded_as_failed_probe() -> None:
    async def run() -> None:
        async def slow(_request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            await asyncio.sleep(0.05)
            return (_probe(AcquisitionOutcome.SUCCEEDED, response=_response()),)

        strategy = FakeStrategy("slow", slow, timeout_seconds=0.005)
        result = await ArticleAcquisitionOrchestrator(
            (strategy,),
            deadline_seconds=1.0,
            max_attempts=1,
            per_domain_concurrency=1,
        ).run(_request())

        assert result.outcome is AcquisitionOutcome.FAILED
        assert result.probes[0].outcome is AcquisitionOutcome.FAILED
        assert result.probes[0].reason_code == "strategy_timeout"
        assert result.probes[0].response is None
        assert result.attempts == 1

    asyncio.run(run())


def test_global_deadline_returns_partial_diagnostics_and_no_success() -> None:
    async def run() -> None:
        async def slow(_request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            await asyncio.sleep(0.1)
            return (_probe(AcquisitionOutcome.SUCCEEDED, response=_response()),)

        strategy = FakeStrategy("slow", slow, timeout_seconds=1.0)
        result = await ArticleAcquisitionOrchestrator(
            (strategy,),
            deadline_seconds=0.01,
            max_attempts=2,
            per_domain_concurrency=1,
        ).run(_request())

        assert result.deadline_exceeded is True
        assert result.outcome is AcquisitionOutcome.FAILED
        assert result.winner is None
        assert result.reason_code == "global_deadline_exceeded"
        assert len(result.probes) == 1
        assert result.probes[0].reason_code == "global_deadline_exceeded"
        assert result.probes[0].response is None
        assert result.attempts == 1

    asyncio.run(run())


def test_attempt_budget_limits_attempt_probes() -> None:
    async def run() -> None:
        first = FakeStrategy(
            "first",
            _handler(
                _probe(AcquisitionOutcome.FAILED),
                _probe(AcquisitionOutcome.REJECTED),
            ),
        )
        second = FakeStrategy("second", _handler(_probe(AcquisitionOutcome.SUCCEEDED)))
        result = await ArticleAcquisitionOrchestrator(
            (first, second),
            deadline_seconds=1.0,
            max_attempts=2,
            per_domain_concurrency=1,
        ).run(_request())

        assert len(result.probes) == 2
        assert result.attempts == 2
        assert first.calls == 1
        assert second.calls == 0

    asyncio.run(run())


def test_configuration_skips_do_not_consume_attempt_budget() -> None:
    async def run() -> None:
        disabled = tuple(
            FakeStrategy(f"disabled-{index}", _handler(), enabled=False) for index in range(3)
        )
        success = _probe(AcquisitionOutcome.SUCCEEDED, response=_response())
        enabled = FakeStrategy("enabled", _handler(success))
        result = await ArticleAcquisitionOrchestrator(
            (*disabled, enabled),
            deadline_seconds=1.0,
            max_attempts=1,
            per_domain_concurrency=1,
        ).run(_request())

        assert len(result.probes) == 4
        assert all(probe.outcome is AcquisitionOutcome.SKIPPED for probe in result.probes[:3])
        assert result.winner is success
        assert enabled.calls == 1
        assert result.attempts == 1

    asyncio.run(run())


def test_cancellation_propagates_before_strategy_execution() -> None:
    async def run() -> None:
        strategy = FakeStrategy("must-not-run", _handler())

        async def cancel() -> None:
            raise JobCancelledError

        with pytest.raises(JobCancelledError):
            await ArticleAcquisitionOrchestrator(
                (strategy,),
                deadline_seconds=1.0,
                max_attempts=1,
                per_domain_concurrency=1,
            ).run(_request(), cancellation_check=cancel)
        assert strategy.calls == 0

    asyncio.run(run())


def test_per_hostname_fetches_respect_concurrency_ceiling() -> None:
    async def run() -> None:
        active = 0
        maximum = 0

        async def fetch(
            url: str, headers: dict[str, str], timeout_seconds: float, allow_binary: bool
        ) -> CollectedResponse:
            nonlocal active, maximum
            del headers, timeout_seconds, allow_binary
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.sleep(0.01)
                return _response(url)
            finally:
                active -= 1

        async def parallel(request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            await asyncio.gather(
                *(
                    request.fetch(f"https://example.com/{index}", {}, 1.0, False)
                    for index in range(6)
                )
            )
            return (_probe(AcquisitionOutcome.FAILED),)

        strategy = FakeStrategy("parallel", parallel)
        result = await ArticleAcquisitionOrchestrator(
            (strategy,),
            deadline_seconds=1.0,
            max_attempts=6,
            per_domain_concurrency=2,
        ).run(_request(fetch))

        assert maximum == 2
        assert result.attempts == 6

    asyncio.run(run())


def test_success_without_response_is_not_promoted_or_fabricated() -> None:
    async def run() -> None:
        malformed_success = _probe(AcquisitionOutcome.SUCCEEDED)
        result = await ArticleAcquisitionOrchestrator(
            (FakeStrategy("missing-response", _handler(malformed_success)),),
            deadline_seconds=1.0,
            max_attempts=1,
            per_domain_concurrency=1,
        ).run(_request())

        assert result.winner is None
        assert result.probes == (malformed_success,)
        assert result.probes[0].response is None
        assert result.outcome is AcquisitionOutcome.FAILED
        assert result.reason_code == "no_accepted_response"

    asyncio.run(run())


def test_empty_strategy_output_does_not_claim_no_strategies_are_configured() -> None:
    async def run() -> None:
        result = await ArticleAcquisitionOrchestrator(
            (FakeStrategy("empty", _handler()),),
            deadline_seconds=1.0,
            max_attempts=1,
            per_domain_concurrency=1,
        ).run(_request())

        assert result.probes == ()
        assert result.winner is None
        assert result.attempts == 0
        assert result.outcome is AcquisitionOutcome.FAILED
        assert result.reason_code == "no_probes_returned"

    asyncio.run(run())


def test_probe_outcomes_and_observation_default_preserve_legacy_strings() -> None:
    probe = _probe("lookup_succeeded")
    metadata = _probe("metadata_succeeded")
    archived = _probe("archived")
    assert probe.outcome is AcquisitionOutcome.LOOKUP_SUCCEEDED
    assert probe.outcome == "lookup_succeeded"
    assert metadata.outcome is AcquisitionOutcome.METADATA_SUCCEEDED
    assert metadata.outcome == "metadata_succeeded"
    assert archived.outcome is AcquisitionOutcome.ARCHIVED
    assert archived.outcome == "archived"
    assert probe.observation_kind is AcquisitionObservationKind.HTTP_RESPONSE


def test_success_with_denied_policy_decision_is_not_accepted() -> None:
    async def run() -> None:
        denied = _probe(
            AcquisitionOutcome.SUCCEEDED,
            response=_response(),
            policy_decision=AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY,
        )
        result = await ArticleAcquisitionOrchestrator(
            (FakeStrategy("denied", _handler(denied)),),
            deadline_seconds=1.0,
            max_attempts=1,
            per_domain_concurrency=1,
        ).run(_request())
        assert result.winner is None
        assert result.probes == (denied,)

    asyncio.run(run())


def test_unexpected_strategy_exception_is_recorded_and_later_strategies_run() -> None:
    async def run() -> None:
        async def explode(_request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            raise RuntimeError("offline failure")

        recovered_probe = _probe(AcquisitionOutcome.FAILED, reason_code="not_found")
        broken = FakeStrategy("broken", explode)
        recovered = FakeStrategy("recovered", _handler(recovered_probe))
        result = await ArticleAcquisitionOrchestrator(
            (broken, recovered),
            deadline_seconds=1.0,
            max_attempts=2,
            per_domain_concurrency=1,
        ).run(_request())

        assert result.probes[0].reason_code == "strategy_error:RuntimeError"
        assert result.probes[1] is recovered_probe
        assert recovered.calls == 1
        assert result.attempts == 2

    asyncio.run(run())


def test_global_timeout_keeps_probes_from_earlier_strategies() -> None:
    async def run() -> None:
        prior_probe = _probe(AcquisitionOutcome.FAILED, reason_code="not_found")

        async def block_until_deadline(
            _request: AcquisitionRequest,
        ) -> Sequence[AcquisitionProbe]:
            await asyncio.Event().wait()

        result = await ArticleAcquisitionOrchestrator(
            (
                FakeStrategy("first", _handler(prior_probe)),
                FakeStrategy("slow", block_until_deadline, timeout_seconds=1.0),
            ),
            deadline_seconds=0.01,
            max_attempts=3,
            per_domain_concurrency=1,
        ).run(_request())

        assert result.deadline_exceeded is True
        assert result.reason_code == "global_deadline_exceeded"
        assert result.probes[0] is prior_probe
        assert result.probes[1].reason_code == "global_deadline_exceeded"
        assert result.attempts == 2

    asyncio.run(run())


def test_fetches_are_bounded_by_remaining_attempt_budget() -> None:
    async def run() -> None:
        fetch_urls: list[str] = []

        async def fetch(
            url: str, headers: dict[str, str], timeout_seconds: float, allow_binary: bool
        ) -> CollectedResponse:
            del headers, timeout_seconds, allow_binary
            fetch_urls.append(url)
            return _response(url)

        prior_probe = _probe(AcquisitionOutcome.FAILED, reason_code="prior_failure")
        must_not_run = FakeStrategy("must-not-run", _handler(_probe(AcquisitionOutcome.SUCCEEDED)))

        async def over_budget(request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            for index in range(4):
                await request.fetch(f"https://example.com/{index}", {}, 1.0, False)
            return (_probe(AcquisitionOutcome.FAILED),)

        result = await ArticleAcquisitionOrchestrator(
            (
                FakeStrategy("prior", _handler(prior_probe)),
                FakeStrategy("many-fetches", over_budget),
                must_not_run,
            ),
            deadline_seconds=1.0,
            max_attempts=2,
            per_domain_concurrency=2,
        ).run(_request(fetch))

        assert len(fetch_urls) == 1
        assert result.probes[0] is prior_probe
        assert result.probes[-1].reason_code == "attempt_budget_exceeded"
        assert result.winner is None
        assert result.attempts == 2
        assert must_not_run.calls == 0

    asyncio.run(run())


def test_multi_fetch_strategy_succeeds_when_budget_is_sufficient() -> None:
    async def run() -> None:
        fetch_urls: list[str] = []

        async def fetch(
            url: str, headers: dict[str, str], timeout_seconds: float, allow_binary: bool
        ) -> CollectedResponse:
            del headers, timeout_seconds, allow_binary
            fetch_urls.append(url)
            return _response(url)

        async def acquire(request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            for index in range(3):
                await request.fetch(f"https://example.com/{index}", {}, 1.0, False)
            return (_probe(AcquisitionOutcome.FAILED),)

        result = await ArticleAcquisitionOrchestrator(
            (FakeStrategy("multi-fetch", acquire),),
            deadline_seconds=1.0,
            max_attempts=3,
            per_domain_concurrency=2,
        ).run(_request(fetch))

        assert len(fetch_urls) == 3
        assert result.probes[0].outcome is AcquisitionOutcome.FAILED
        assert result.probes[0].reason_code is None
        assert result.attempts == 3

    asyncio.run(run())


def test_cancellation_after_strategy_returns_success_propagates() -> None:
    async def run() -> None:
        checks = 0

        async def fetch(
            url: str, headers: dict[str, str], timeout_seconds: float, allow_binary: bool
        ) -> CollectedResponse:
            del headers, timeout_seconds, allow_binary
            return _response(url)

        async def successful_acquire(request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            response = await request.fetch("https://example.com/article", {}, 1.0, False)
            return (_probe(AcquisitionOutcome.SUCCEEDED, response=response),)

        async def cancel_after_return() -> None:
            nonlocal checks
            checks += 1
            if checks == 4:
                raise JobCancelledError

        with pytest.raises(JobCancelledError):
            await ArticleAcquisitionOrchestrator(
                (FakeStrategy("final", successful_acquire),),
                deadline_seconds=1.0,
                max_attempts=2,
                per_domain_concurrency=1,
            ).run(_request(fetch), cancellation_check=cancel_after_return)

        assert checks == 4

    asyncio.run(run())


def test_domain_limiter_is_shared_across_runs_and_normalizes_hostnames() -> None:
    async def run() -> None:
        active = 0
        maximum = 0
        entered_handlers = 0
        both_handlers_entered = asyncio.Event()
        first_fetch_started = asyncio.Event()
        release_first_fetch = asyncio.Event()

        async def fetch(
            url: str, headers: dict[str, str], timeout_seconds: float, allow_binary: bool
        ) -> CollectedResponse:
            nonlocal active, maximum
            del headers, timeout_seconds, allow_binary
            active += 1
            maximum = max(maximum, active)
            first_fetch_started.set()
            try:
                await release_first_fetch.wait()
                return _response(url)
            finally:
                active -= 1

        async def acquire(request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
            nonlocal entered_handlers
            entered_handlers += 1
            if entered_handlers == 2:
                both_handlers_entered.set()
            suffix = "first" if entered_handlers == 1 else "second"
            await request.fetch(f"https://EXAMPLE.COM./{suffix}", {}, 1.0, False)
            return (_probe(AcquisitionOutcome.FAILED),)

        orchestrator = ArticleAcquisitionOrchestrator(
            (FakeStrategy("parallel-runs", acquire),),
            deadline_seconds=1.0,
            max_attempts=2,
            per_domain_concurrency=1,
        )
        first = asyncio.create_task(orchestrator.run(_request(fetch)))
        second = asyncio.create_task(orchestrator.run(_request(fetch)))
        await asyncio.wait_for(both_handlers_entered.wait(), timeout=0.5)
        await asyncio.wait_for(first_fetch_started.wait(), timeout=0.5)
        await asyncio.sleep(0.01)
        assert maximum == 1
        release_first_fetch.set()
        await asyncio.gather(first, second)
        assert maximum == 1

        limiter = DomainConcurrencyLimiter(1)
        standard = limiter.semaphore_for("https://example.com/")
        assert standard is limiter.semaphore_for("https://EXAMPLE.COM./story")
        assert limiter.semaphore_for("https://exämple.com/") is limiter.semaphore_for(
            "https://xn--exmple-cua.com/"
        )

    asyncio.run(run())


def test_domain_limiter_does_not_reuse_semaphores_between_event_loops() -> None:
    limiter = DomainConcurrencyLimiter(1)

    async def semaphore() -> asyncio.Semaphore:
        return limiter.semaphore_for("https://example.com/")

    first = asyncio.run(semaphore())
    second = asyncio.run(semaphore())
    assert first is not second


def test_probe_rejects_evidence_stronger_than_its_equivalence_class() -> None:
    with pytest.raises(ValueError, match="stronger than the equivalence class"):
        _probe(
            AcquisitionOutcome.SUCCEEDED,
            equivalence_class=SourceEquivalenceClass.INDEPENDENT_CORROBORATION,
            evidence_class=EvidenceClass.PRIMARY_OBSERVED,
        )


def test_probe_accepts_conservative_evidence_for_equivalence_class() -> None:
    probe = _probe(
        AcquisitionOutcome.SUCCEEDED,
        equivalence_class=SourceEquivalenceClass.EXACT_ORIGINAL,
        evidence_class=EvidenceClass.UNKNOWN,
    )
    assert probe.evidence_class is EvidenceClass.UNKNOWN


def test_empty_registry_has_no_strategies_reason() -> None:
    async def run() -> None:
        result = await ArticleAcquisitionOrchestrator(
            (),
            deadline_seconds=1.0,
            max_attempts=1,
            per_domain_concurrency=1,
        ).run(_request())
        assert result.outcome is AcquisitionOutcome.NOT_CONFIGURED
        assert result.reason_code == "no_strategies_configured"

    asyncio.run(run())

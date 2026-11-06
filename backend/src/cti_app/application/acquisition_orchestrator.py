"""Bounded execution for ordered article acquisition strategies."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit

from cti_app.application.acquisition_fallbacks import (
    AcquisitionProbe,
    AcquisitionRequest,
)
from cti_app.application.http_collection import CollectedResponse
from cti_app.application.jobs import JobCancelledError
from cti_app.domain.acquisition import (
    AcquisitionCapabilityState,
    AcquisitionOutcome,
    AcquisitionPolicyDecision,
)
from cti_app.domain.collection import SourceAcquisitionMethod


class _Strategy(Protocol):
    name: str
    version: str
    method: SourceAcquisitionMethod
    enabled: bool
    timeout_seconds: float

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]: ...


CancellationCheck = Callable[[], Awaitable[None] | None]
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class AcquisitionRunResult:
    outcome: AcquisitionOutcome
    probes: tuple[AcquisitionProbe, ...]
    winner: AcquisitionProbe | None
    reason_code: str | None
    attempts: int
    deadline_exceeded: bool
    cancelled: bool


class ArticleAcquisitionOrchestrator:
    def __init__(
        self,
        strategies: Sequence[_Strategy],
        *,
        deadline_seconds: float,
        max_attempts: int,
        per_domain_concurrency: int,
        clock: Clock = monotonic,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if per_domain_concurrency < 1:
            raise ValueError("per_domain_concurrency must be positive")
        self._strategies = tuple(strategies)
        self._deadline_seconds = deadline_seconds
        self._max_attempts = max_attempts
        self._per_domain_concurrency = per_domain_concurrency
        self._clock = clock

    async def run(
        self,
        request: AcquisitionRequest,
        *,
        cancellation_check: CancellationCheck | None = None,
    ) -> AcquisitionRunResult:
        probes: list[AcquisitionProbe] = []
        attempts = 0
        winner: AcquisitionProbe | None = None
        semaphores: dict[str, asyncio.Semaphore] = {}
        active_strategy: _Strategy | None = None
        active_started: float | None = None

        async def bounded_fetch(
            url: str,
            headers: dict[str, str],
            timeout_seconds: float,
            allow_binary: bool,
        ) -> CollectedResponse:
            try:
                hostname = (urlsplit(url).hostname or "").casefold()
            except ValueError:
                hostname = ""
            semaphore = semaphores.setdefault(
                hostname, asyncio.Semaphore(self._per_domain_concurrency)
            )
            async with semaphore:
                return await request.fetch(url, headers, timeout_seconds, allow_binary)

        bounded_request = replace(request, fetch=bounded_fetch)
        global_timeout = asyncio.timeout(self._deadline_seconds)
        try:
            async with global_timeout:
                for strategy in self._strategies:
                    if attempts >= self._max_attempts:
                        break
                    if cancellation_check is not None:
                        cancellation_result = cancellation_check()
                        if inspect.isawaitable(cancellation_result):
                            await cancellation_result

                    capability = getattr(
                        strategy,
                        "capability_state",
                        AcquisitionCapabilityState.IMPLEMENTED,
                    )
                    if capability is None:
                        capability = AcquisitionCapabilityState.IMPLEMENTED
                    enabled = getattr(strategy, "enabled", True)
                    if (
                        not enabled
                        or capability is AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
                    ):
                        probes.append(
                            self._probe(
                                strategy,
                                request,
                                outcome=AcquisitionOutcome.SKIPPED,
                                reason_code="disabled_by_configuration",
                                capability_state=AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION,
                            )
                        )
                        continue
                    if capability is not AcquisitionCapabilityState.IMPLEMENTED:
                        outcome, policy_decision = _outcome_for_capability(capability)
                        probes.append(
                            self._probe(
                                strategy,
                                request,
                                outcome=outcome,
                                reason_code=capability.value,
                                capability_state=capability,
                                policy_decision=policy_decision,
                            )
                        )
                        continue

                    started = self._clock()
                    active_strategy = strategy
                    active_started = started
                    try:
                        strategy_timeout = float(strategy.timeout_seconds)
                        if strategy_timeout <= 0:
                            raise ValueError("strategy timeout must be positive")
                        async with asyncio.timeout(strategy_timeout):
                            strategy_probes = await strategy.acquire(bounded_request)
                    except JobCancelledError:
                        raise
                    except TimeoutError:
                        if global_timeout.expired():
                            raise
                        probes.append(
                            self._probe(
                                strategy,
                                request,
                                outcome=AcquisitionOutcome.FAILED,
                                reason_code="strategy_timeout",
                                duration_ms=self._duration_ms(started),
                            )
                        )
                        attempts += 1
                        active_strategy = None
                        active_started = None
                        continue
                    except Exception as error:
                        probes.append(
                            self._probe(
                                strategy,
                                request,
                                outcome=AcquisitionOutcome.FAILED,
                                reason_code=f"strategy_error:{type(error).__name__}",
                                duration_ms=self._duration_ms(started),
                            )
                        )
                        attempts += 1
                        active_strategy = None
                        active_started = None
                        continue
                    active_strategy = None
                    active_started = None

                    for probe in tuple(strategy_probes):
                        if _is_attempt_probe(probe):
                            if attempts >= self._max_attempts:
                                continue
                            attempts += 1
                        probes.append(probe)
                        if _is_accepted_success(probe):
                            winner = probe
                            break
                    if winner is not None:
                        break
        except TimeoutError:
            if global_timeout.expired():
                if active_strategy is not None and attempts < self._max_attempts:
                    probes.append(
                        self._probe(
                            active_strategy,
                            request,
                            outcome=AcquisitionOutcome.FAILED,
                            reason_code="global_deadline_exceeded",
                            duration_ms=(
                                self._duration_ms(active_started)
                                if active_started is not None
                                else 0
                            ),
                        )
                    )
                    attempts += 1
                return AcquisitionRunResult(
                    outcome=AcquisitionOutcome.FAILED,
                    probes=tuple(probes),
                    winner=None,
                    reason_code="global_deadline_exceeded",
                    attempts=attempts,
                    deadline_exceeded=True,
                    cancelled=False,
                )
            raise

        if winner is not None:
            return AcquisitionRunResult(
                outcome=AcquisitionOutcome.SUCCEEDED,
                probes=tuple(probes),
                winner=winner,
                reason_code=None,
                attempts=attempts,
                deadline_exceeded=False,
                cancelled=False,
            )

        return AcquisitionRunResult(
            outcome=_final_outcome(probes),
            probes=tuple(probes),
            winner=None,
            reason_code=_final_reason(probes),
            attempts=attempts,
            deadline_exceeded=False,
            cancelled=False,
        )

    def _probe(
        self,
        strategy: _Strategy,
        request: AcquisitionRequest,
        *,
        outcome: AcquisitionOutcome,
        reason_code: str,
        capability_state: AcquisitionCapabilityState | None = None,
        policy_decision: AcquisitionPolicyDecision = AcquisitionPolicyDecision.ALLOWED,
        duration_ms: int = 0,
    ) -> AcquisitionProbe:
        return AcquisitionProbe(
            strategy_name=strategy.name,
            strategy_version=strategy.version,
            method=strategy.method,
            requested_url=request.requested_url,
            outcome=outcome,
            reason_code=reason_code,
            response=None,
            http_status=None,
            encoded_size=None,
            encoded_sha256=None,
            duration_ms=duration_ms,
            capability_state=capability_state or AcquisitionCapabilityState.IMPLEMENTED,
            policy_decision=policy_decision,
        )

    def _duration_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))


def _is_accepted_success(probe: AcquisitionProbe) -> bool:
    return (
        probe.outcome == AcquisitionOutcome.SUCCEEDED
        and probe.response is not None
        and probe.policy_decision is AcquisitionPolicyDecision.ALLOWED
    )


def _is_attempt_probe(probe: AcquisitionProbe) -> bool:
    return probe.outcome in {
        AcquisitionOutcome.SUCCEEDED,
        AcquisitionOutcome.FAILED,
        AcquisitionOutcome.REJECTED,
        AcquisitionOutcome.NEEDS_RECONCILIATION,
        AcquisitionOutcome.LOOKUP_SUCCEEDED,
        AcquisitionOutcome.METADATA_SUCCEEDED,
        AcquisitionOutcome.ARCHIVED,
    }


def _outcome_for_capability(
    capability: AcquisitionCapabilityState,
) -> tuple[AcquisitionOutcome, AcquisitionPolicyDecision]:
    if capability in {
        AcquisitionCapabilityState.BLOCKED_BY_POLICY,
        AcquisitionCapabilityState.BLOCKED_BY_SECURITY,
    }:
        decision = (
            AcquisitionPolicyDecision.DENIED_PRIVATE_DESTINATION
            if capability is AcquisitionCapabilityState.BLOCKED_BY_SECURITY
            else AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY
        )
        return AcquisitionOutcome.POLICY_SKIPPED, decision
    return AcquisitionOutcome.NOT_CONFIGURED, AcquisitionPolicyDecision.NOT_CONFIGURED


def _final_outcome(probes: Sequence[AcquisitionProbe]) -> AcquisitionOutcome:
    if not probes:
        return AcquisitionOutcome.NOT_CONFIGURED
    outcomes = {probe.outcome for probe in probes}
    if outcomes <= {AcquisitionOutcome.SKIPPED, AcquisitionOutcome.NOT_CONFIGURED}:
        return AcquisitionOutcome.SKIPPED
    if outcomes == {AcquisitionOutcome.POLICY_SKIPPED}:
        return AcquisitionOutcome.POLICY_SKIPPED
    return AcquisitionOutcome.FAILED


def _final_reason(probes: Sequence[AcquisitionProbe]) -> str | None:
    if not probes:
        return "no_strategies_configured"
    for probe in reversed(probes):
        if probe.reason_code:
            return probe.reason_code
    return "no_accepted_response"

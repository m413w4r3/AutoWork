"""Automatic recovery policy for the first production pass."""

from __future__ import annotations

from enum import StrEnum

from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    EditionProductionBatchItem,
    SubjectProductionRun,
    SubjectProductionStatus,
)


class ProductionRecoveryDisposition(StrEnum):
    AUTO = "auto"
    MANUAL_ONLY = "manual_only"


class ProductionRecoveryPolicyV1:
    """Allow exactly one automatic retry for known operational failures."""

    Q2_SOURCE_COVERAGE_ERROR_CODE = "q2_source_coverage_failed"
    _Q2_UNSAFE_FAILURE_CLASSES = frozenset(
        {
            "reconciliation_required",
            "control_invariant_failure",
        }
    )

    MANUAL_ONLY_ERROR_CODES = frozenset(
        {
            # A provider request may already exist outside our database. No
            # automatic retry can safely resolve that ambiguity.
            "model_submission_reconciliation_required",
        }
    )

    AUTO_ERROR_CODES = frozenset(
        {
            "bridge_server_error",
            "bridge_idle_timeout",
            "bridge_total_timeout",
            "bridge_timeout",
            "bridge_ui_timeout",
            "bridge_extension_disconnected",
            "bridge_unreachable",
            "bridge_rate_limited",
            "conversation_unavailable",
            "conversation_profile_mismatch",
            "conversation_busy",
            "no_model_response",
            "references_format_unusable",
            "synthesis_validation_failed",
        }
    )

    # Short aliases make the policy useful to callers that need to render or
    # audit the decision without duplicating the allow-list.
    AUTO = ProductionRecoveryDisposition.AUTO
    MANUAL_ONLY = ProductionRecoveryDisposition.MANUAL_ONLY

    @classmethod
    def disposition(cls, error_code: str | None) -> ProductionRecoveryDisposition:
        if error_code in cls.MANUAL_ONLY_ERROR_CODES:
            return cls.MANUAL_ONLY
        if error_code in cls.AUTO_ERROR_CODES:
            return cls.AUTO
        return cls.MANUAL_ONLY

    @classmethod
    def is_auto_recoverable(cls, error_code: str | None) -> bool:
        return cls.disposition(error_code) is cls.AUTO

    @classmethod
    def disposition_for_run(cls, run: SubjectProductionRun) -> ProductionRecoveryDisposition:
        """Return the recovery disposition without losing run-local details."""
        if run.error_code == cls.Q2_SOURCE_COVERAGE_ERROR_CODE:
            if run.reconciliation is not None:
                return cls.MANUAL_ONLY
            return (
                cls.AUTO
                if cls._all_q2_blocking_failures_retryable(run)
                else cls.MANUAL_ONLY
            )
        # A transient transport error can be the aggregate code after a Q2
        # batch/attempt stopped early.  In that shape the aggregate code alone
        # would incorrectly hide a terminal source failure from the policy.
        # Once source-local failures are present, every blocking one must be
        # explicitly retryable before the batch may open a new generation.
        details = run.error_details
        source_failures = details.get("source_failures") if isinstance(details, dict) else None
        has_blocking_failure = isinstance(source_failures, dict) and any(
            not isinstance(failure, dict)
            or failure.get("contributes_to_coverage", True) is not False
            for failure in source_failures.values()
        )
        if has_blocking_failure:
            return (
                cls.AUTO
                if cls._all_q2_blocking_failures_retryable(run)
                else cls.MANUAL_ONLY
            )
        return cls.disposition(run.error_code)

    @classmethod
    def current_stage_retry_recommended(
        cls, run: SubjectProductionRun
    ) -> bool:
        """Whether replaying the stage that stopped the run is recommended."""
        return (
            run.status
            in {
                SubjectProductionStatus.FAILED,
                SubjectProductionStatus.NEEDS_REVIEW,
            }
            and run.current_stage is not None
            and cls.disposition_for_run(run) is cls.AUTO
        )

    @classmethod
    def _all_q2_blocking_failures_retryable(cls, run: SubjectProductionRun) -> bool:
        details = run.error_details
        if not isinstance(details, dict):
            return False

        failures = details.get("source_failures")
        if not isinstance(failures, dict) or not failures:
            return False

        blocking: list[dict[object, object]] = []
        for failure in failures.values():
            if not isinstance(failure, dict):
                return False

            failure_class = failure.get("failure_class")
            if failure_class is not None:
                if not isinstance(failure_class, str):
                    return False
                if failure_class in cls._Q2_UNSAFE_FAILURE_CLASSES:
                    return False
            if (
                failure.get("error_code") == PRODUCTION_RECONCILIATION_ERROR_CODE
                or failure.get("phase") == "reconciliation"
            ):
                return False

            retryable = failure.get("retryable")
            if retryable is not True and retryable is not False:
                return False

            contributes_to_coverage = failure.get("contributes_to_coverage", True)
            if not isinstance(contributes_to_coverage, bool):
                return False
            if contributes_to_coverage is False:
                continue
            blocking.append(failure)

        if not blocking:
            return False

        return all(failure.get("retryable") is True for failure in blocking)

    @classmethod
    def eligible(cls, item: EditionProductionBatchItem, run: SubjectProductionRun) -> bool:
        # Cancellation is an absolute terminal decision.  Keep this explicit
        # even though CANCELLED is not one of the allow-listed statuses: it is
        # a fence against future policy additions accidentally reviving a run.
        if run.status is SubjectProductionStatus.CANCELLED:
            return False
        return (
            item.auto_recovery_count == 0
            and run.status
            in {
                SubjectProductionStatus.FAILED,
                SubjectProductionStatus.NEEDS_REVIEW,
            }
            and run.current_stage is not None
            and cls.disposition_for_run(run) is cls.AUTO
        )


__all__ = [
    "ProductionRecoveryDisposition",
    "ProductionRecoveryPolicyV1",
]

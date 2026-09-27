"""Automatic recovery policy for the first production pass."""

from __future__ import annotations

from enum import StrEnum

from cti_app.domain.production import (
    EditionProductionBatchItem,
    ProductionRun,
    ProductionRunStatus,
)


class ProductionRecoveryDisposition(StrEnum):
    AUTO = "auto"
    MANUAL_ONLY = "manual_only"


class ProductionRecoveryPolicyV1:
    """Allow exactly one automatic retry for known operational failures."""

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
            "bridge_run_unavailable",
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
    def disposition_for_run(cls, run: ProductionRun) -> ProductionRecoveryDisposition:
        """Return the recovery disposition of a stopped run.

        A run awaiting reconciliation is never replayed automatically, whatever
        the code it stopped with.
        """
        if run.reconciliation is not None:
            return cls.MANUAL_ONLY
        return cls.disposition(run.error_code)

    @classmethod
    def current_stage_retry_recommended(cls, run: ProductionRun) -> bool:
        """Whether replaying the stage that stopped the run is recommended."""
        return (
            run.status
            in {
                ProductionRunStatus.FAILED,
                ProductionRunStatus.NEEDS_REVIEW,
            }
            and run.current_stage is not None
            and cls.disposition_for_run(run) is cls.AUTO
        )

    @classmethod
    def eligible(cls, item: EditionProductionBatchItem, run: ProductionRun) -> bool:
        # Cancellation is an absolute terminal decision.  Keep this explicit
        # even though CANCELLED is not one of the allow-listed statuses: it is
        # a fence against future policy additions accidentally reviving a run.
        if run.status is ProductionRunStatus.CANCELLED:
            return False
        return (
            item.auto_recovery_count == 0
            and run.status
            in {
                ProductionRunStatus.FAILED,
                ProductionRunStatus.NEEDS_REVIEW,
            }
            and run.current_stage is not None
            and cls.disposition_for_run(run) is cls.AUTO
        )


__all__ = [
    "ProductionRecoveryDisposition",
    "ProductionRecoveryPolicyV1",
]

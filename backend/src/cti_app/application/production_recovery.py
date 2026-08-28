"""Automatic recovery policy for the first production pass."""

from __future__ import annotations

from enum import StrEnum

from cti_app.domain.production import (
    EditionProductionBatchItem,
    SubjectProductionRun,
    SubjectProductionStatus,
)


class ProductionRecoveryDisposition(StrEnum):
    AUTO = "auto"
    MANUAL_ONLY = "manual_only"


class ProductionRecoveryPolicyV1:
    """Allow exactly one automatic retry for known operational failures."""

    AUTO_ERROR_CODES = frozenset(
        {
            "bridge_server_error",
            "bridge_idle_timeout",
            "bridge_total_timeout",
            "bridge_timeout",
            "bridge_ui_timeout",
            "conversation_unavailable",
            "conversation_profile_mismatch",
            "conversation_busy",
            "no_model_response",
            "references_format_unusable",
            "q2_source_coverage_failed",
            "synthesis_validation_failed",
        }
    )

    # Short aliases make the policy useful to callers that need to render or
    # audit the decision without duplicating the allow-list.
    AUTO = ProductionRecoveryDisposition.AUTO
    MANUAL_ONLY = ProductionRecoveryDisposition.MANUAL_ONLY

    @classmethod
    def disposition(cls, error_code: str | None) -> ProductionRecoveryDisposition:
        if error_code in cls.AUTO_ERROR_CODES:
            return cls.AUTO
        return cls.MANUAL_ONLY

    @classmethod
    def is_auto_recoverable(cls, error_code: str | None) -> bool:
        return cls.disposition(error_code) is cls.AUTO

    @classmethod
    def eligible(cls, item: EditionProductionBatchItem, run: SubjectProductionRun) -> bool:
        return (
            item.auto_recovery_count == 0
            and run.status
            in {
                SubjectProductionStatus.FAILED,
                SubjectProductionStatus.NEEDS_REVIEW,
            }
            and run.current_stage is not None
            and cls.is_auto_recoverable(run.error_code)
        )


__all__ = [
    "ProductionRecoveryDisposition",
    "ProductionRecoveryPolicyV1",
]

"""Pacing policies for the production pipeline.

The policy only samples delays.  Dispatching and sleeping remain the
responsibility of the caller because subject pacing must use the job
dispatcher, while Q2 pacing is deliberately local to the workflow.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from cti_app.config import Settings
from cti_app.domain.production import SubjectProductionStage


class ProductionPacingKind(StrEnum):
    SUBJECT = "subject"
    MODEL = "model"


@dataclass(frozen=True, slots=True)
class ProductionPacingPolicy:
    """Bounded, injectable pacing for one production process."""

    subject_jitter_min_seconds: float = 30.0
    subject_jitter_max_seconds: float = 90.0
    model_jitter_min_seconds: float = 8.0
    model_jitter_max_seconds: float = 20.0
    cooldown_every_n_subjects: int = 3
    cooldown_min_seconds: float = 600.0
    cooldown_max_seconds: float = 1200.0

    def __post_init__(self) -> None:
        for name in (
            "subject_jitter_min_seconds",
            "subject_jitter_max_seconds",
            "model_jitter_min_seconds",
            "model_jitter_max_seconds",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.subject_jitter_max_seconds < self.subject_jitter_min_seconds:
            raise ValueError("subject jitter max must be >= min")
        if self.model_jitter_max_seconds < self.model_jitter_min_seconds:
            raise ValueError("model jitter max must be >= min")
        if self.cooldown_every_n_subjects < 0:
            raise ValueError("cooldown_every_n_subjects must be >= 0")
        if self.cooldown_min_seconds < 0 or self.cooldown_max_seconds < 0:
            raise ValueError("cooldown bounds must be >= 0")
        if self.cooldown_max_seconds < self.cooldown_min_seconds:
            raise ValueError("cooldown max must be >= min")

    @classmethod
    def zero(cls) -> ProductionPacingPolicy:
        """No-op policy used by isolated tests and non-production callers."""
        return cls(
            subject_jitter_min_seconds=0,
            subject_jitter_max_seconds=0,
            model_jitter_min_seconds=0,
            model_jitter_max_seconds=0,
            cooldown_every_n_subjects=0,
            cooldown_min_seconds=0,
            cooldown_max_seconds=0,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> ProductionPacingPolicy:
        return cls(
            subject_jitter_min_seconds=float(settings.production_subject_jitter_min_seconds),
            subject_jitter_max_seconds=float(settings.production_subject_jitter_max_seconds),
            model_jitter_min_seconds=float(settings.production_model_jitter_min_seconds),
            model_jitter_max_seconds=float(settings.production_model_jitter_max_seconds),
            cooldown_every_n_subjects=int(settings.production_cooldown_every_n_subjects),
            cooldown_min_seconds=float(settings.production_cooldown_min_seconds),
            cooldown_max_seconds=float(settings.production_cooldown_max_seconds),
        )

    @staticmethod
    def _sample(minimum: float, maximum: float) -> float:
        if minimum == maximum:
            return minimum
        return random.uniform(minimum, maximum)

    def subject_delay_seconds(self) -> float:
        return self._sample(
            self.subject_jitter_min_seconds,
            self.subject_jitter_max_seconds,
        )

    def model_delay_seconds(self) -> float:
        return self._sample(
            self.model_jitter_min_seconds,
            self.model_jitter_max_seconds,
        )

    def subject_delay_ms(self, *, sequence_index: int = 0) -> int:
        """Delay before starting the subject at ``sequence_index`` (0-based).

        Every ``cooldown_every_n_subjects`` subjects, the pipeline pauses far
        longer than the ordinary jitter. The ChatGPT bridge drives a single
        browser session and degrades measurably after a few hours of
        uninterrupted production.
        """
        if (
            self.cooldown_every_n_subjects > 0
            and sequence_index > 0
            and sequence_index % self.cooldown_every_n_subjects == 0
        ):
            return max(
                0,
                round(
                    self._sample(self.cooldown_min_seconds, self.cooldown_max_seconds) * 1000
                ),
            )
        return max(0, round(self.subject_delay_seconds() * 1000))

    def model_delay_ms(self, stage: SubjectProductionStage | str) -> int:
        """Return a deferred dispatch delay for Q1 or Q4 only."""
        normalized = stage.value if isinstance(stage, SubjectProductionStage) else str(stage)
        if normalized not in {
            SubjectProductionStage.REFERENCES.value,
            SubjectProductionStage.SYNTHESIS.value,
        }:
            return 0
        return max(0, round(self.model_delay_seconds() * 1000))

    @staticmethod
    def delay_until(dispatch_at: datetime | None, *, now: datetime | None = None) -> int:
        if dispatch_at is None:
            return 0
        moment = now or datetime.now(UTC)
        if dispatch_at.tzinfo is None or dispatch_at.utcoffset() is None:
            raise ValueError("dispatch_at must be timezone-aware")
        return max(0, round((dispatch_at - moment).total_seconds() * 1000))


__all__ = ["ProductionPacingKind", "ProductionPacingPolicy"]

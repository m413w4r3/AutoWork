from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class Banality(StrEnum):
    UNKNOWN = "UNKNOWN"
    SPECIFIC = "SPECIFIC"
    SUSPICIOUS_COMMON = "SUSPICIOUS_COMMON"
    BANAL = "BANAL"


@dataclass(frozen=True, slots=True, kw_only=True)
class GoodwareFeature:
    feature_kind: str
    normalized_value: str
    occurrence_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class GoodwareSource:
    filename: str
    feature_kind: str
    sha256: str
    size: int
    blob_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class GoodwareBaseline:
    id: UUID
    source_set_sha256: str
    records_sha256: str
    record_count: int
    occurrence_sum: int
    pattern_version: str
    sources: tuple[GoodwareSource, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class BanalityThresholds:
    suspicious_count: int
    banal_count: int

    def __post_init__(self) -> None:
        if not 1 <= self.suspicious_count <= self.banal_count:
            raise ValueError("thresholds must satisfy 1 <= suspicious_count <= banal_count")


class BanalityScorer:
    def __init__(self, thresholds: BanalityThresholds) -> None:
        self.thresholds = thresholds

    def score(self, occurrence_count: int | None) -> Banality:
        if occurrence_count is None:
            return Banality.UNKNOWN
        if occurrence_count < 1:
            raise ValueError("occurrence_count must be positive")
        if occurrence_count < self.thresholds.suspicious_count:
            return Banality.SPECIFIC
        if occurrence_count < self.thresholds.banal_count:
            return Banality.SUSPICIOUS_COMMON
        return Banality.BANAL


class GoodwareBaselineError(ValueError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class DeclaredNonDiscriminant:
    category: str
    feature_kind: str
    normalized_value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MeasuredGoodwareCount:
    feature_kind: str
    normalized_value: str
    occurrence_count: int


def validate_features(features: Iterable[GoodwareFeature]) -> None:
    previous: tuple[str, str] | None = None
    for feature in features:
        if feature.occurrence_count < 1:
            raise GoodwareBaselineError("feature occurrence_count must be positive")
        key = (feature.feature_kind, feature.normalized_value)
        if previous is not None and key <= previous:
            raise GoodwareBaselineError("features must be strictly sorted and unique")
        previous = key

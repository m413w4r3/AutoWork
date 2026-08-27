from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
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


NON_DISCRIMINANT_SCHEMA_VERSION = "non-discriminant-patterns-v1"
NON_DISCRIMINANT_CATEGORIES = (
    "standard_sections",
    "go_runtime",
    "msvc_crt",
    "dotnet_metadata",
    "upx",
    "delphi",
)


class NonDiscriminantPatternRegistry:
    def __init__(self, entries: tuple[DeclaredNonDiscriminant, ...]) -> None:
        self.entries = entries
        self._lookup = {(entry.feature_kind, entry.normalized_value): entry for entry in entries}

    def lookup(self, feature_kind: str, normalized_value: str) -> DeclaredNonDiscriminant | None:
        return self._lookup.get((feature_kind, normalized_value))

    def get(self, feature_kind: str, normalized_value: str) -> DeclaredNonDiscriminant | None:
        return self.lookup(feature_kind, normalized_value)

    def __iter__(self) -> Iterator[DeclaredNonDiscriminant]:
        return iter(self.entries)


def load_non_discriminant_patterns(
    path: Path | None = None,
) -> NonDiscriminantPatternRegistry:
    pattern_path = (
        path or Path(__file__).resolve().parents[1] / "data" / "non_discriminant_patterns_v1.json"
    )
    try:
        document = json.loads(pattern_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoodwareBaselineError("invalid non-discriminant pattern registry") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "categories"}
        or document.get("schema_version") != NON_DISCRIMINANT_SCHEMA_VERSION
    ):
        raise GoodwareBaselineError("unsupported non-discriminant pattern schema")
    categories = document.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(NON_DISCRIMINANT_CATEGORIES):
        raise GoodwareBaselineError("non-discriminant categories are not exact")

    entries: list[DeclaredNonDiscriminant] = []
    seen: set[tuple[str, str]] = set()
    for category in NON_DISCRIMINANT_CATEGORIES:
        values = categories[category]
        if not isinstance(values, list):
            raise GoodwareBaselineError("non-discriminant category must be a list")
        for value in values:
            if not isinstance(value, dict) or set(value) != {"feature_kind", "normalized_value"}:
                raise GoodwareBaselineError("invalid non-discriminant entry")
            feature_kind = value["feature_kind"]
            normalized_value = value["normalized_value"]
            if (
                not isinstance(feature_kind, str)
                or not feature_kind
                or feature_kind != feature_kind.lower()
                or not isinstance(normalized_value, str)
                or not normalized_value
                or normalized_value != normalized_value.lower()
            ):
                raise GoodwareBaselineError(
                    "non-discriminant values must be non-empty lowercase strings"
                )
            key = (feature_kind, normalized_value)
            if key in seen:
                raise GoodwareBaselineError("duplicate non-discriminant entry")
            seen.add(key)
            entries.append(
                DeclaredNonDiscriminant(
                    category=category,
                    feature_kind=feature_kind,
                    normalized_value=normalized_value,
                )
            )
    return NonDiscriminantPatternRegistry(tuple(entries))


def validate_features(features: Iterable[GoodwareFeature]) -> None:
    previous: tuple[str, str] | None = None
    for feature in features:
        if feature.occurrence_count < 1:
            raise GoodwareBaselineError("feature occurrence_count must be positive")
        key = (feature.feature_kind, feature.normalized_value)
        if previous is not None and key <= previous:
            raise GoodwareBaselineError("features must be strictly sorted and unique")
        previous = key

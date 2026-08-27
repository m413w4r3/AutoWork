from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from cti_app.domain.blobs import utc_now


class ReferenceLabelSource(StrEnum):
    ANALYST = "ANALYST"
    OPERATOR_IMPORT = "OPERATOR_IMPORT"


class ReferenceCorpusVerdict(StrEnum):
    MULTI_FAMILY = "MULTI_FAMILY"
    FAMILY_SPECIFIC = "FAMILY_SPECIFIC"
    CORPUS_TOO_SMALL = "CORPUS_TOO_SMALL"
    UNKNOWN = "UNKNOWN"


def normalize_family_label(value: str) -> str:
    value = value.strip().lower()
    if not value:
        raise ValueError("family_label cannot be empty")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceMember:
    sample_id: UUID
    sample_sha256: str
    family_label: str
    origin_investigation_id: UUID | None
    actor_id: str
    label_source: ReferenceLabelSource
    promoted_at: datetime = field(default_factory=utc_now)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.sample_sha256.strip():
            raise ValueError("sample_sha256 cannot be empty")
        if not self.actor_id.strip():
            raise ValueError("actor_id cannot be empty")
        object.__setattr__(self, "family_label", normalize_family_label(self.family_label))
        try:
            object.__setattr__(self, "label_source", ReferenceLabelSource(self.label_source))
        except ValueError as exc:
            raise ValueError("invalid reference label source") from exc
        if self.promoted_at.tzinfo is None or self.promoted_at.utcoffset() is None:
            raise ValueError("promoted_at must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceMemberDispute:
    member_id: UUID
    reason: str
    actor_id: str
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.reason.strip() or not self.actor_id.strip():
            raise ValueError("reason and actor_id cannot be empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferenceCorpusAssessment:
    verdict: ReferenceCorpusVerdict
    feature_kind: str
    normalized_value: str
    malware_sample_count: int
    family_sample_counts: dict[str, int]
    benign_sample_occurrences: int


def assess_reference_feature(
    *,
    feature_kind: str,
    normalized_value: str,
    malware_members: Iterable[tuple[UUID, str]],
    benign_sample_occurrences: int,
    total_eligible_samples_by_family: Mapping[str, int],
    min_family_samples: int = 5,
) -> ReferenceCorpusAssessment:
    if min_family_samples < 1:
        raise ValueError("min_family_samples must be positive")
    families: dict[str, set[UUID]] = {}
    for sample_id, label in malware_members:
        families.setdefault(normalize_family_label(label), set()).add(sample_id)
    family_counts = {label: len(samples) for label, samples in families.items()}
    malware_count = sum(family_counts.values())
    if len(family_counts) >= 2:
        verdict = ReferenceCorpusVerdict.MULTI_FAMILY
    elif len(family_counts) == 1:
        family = next(iter(family_counts))
        family_size = total_eligible_samples_by_family.get(family, 0)
        verdict = (
            ReferenceCorpusVerdict.FAMILY_SPECIFIC
            if family_size >= min_family_samples
            else ReferenceCorpusVerdict.CORPUS_TOO_SMALL
        )
    else:
        total_eligible = sum(total_eligible_samples_by_family.values())
        verdict = (
            ReferenceCorpusVerdict.UNKNOWN
            if total_eligible >= min_family_samples
            else ReferenceCorpusVerdict.CORPUS_TOO_SMALL
        )
    return ReferenceCorpusAssessment(
        verdict=verdict,
        feature_kind=feature_kind,
        normalized_value=normalized_value,
        malware_sample_count=malware_count,
        family_sample_counts=family_counts,
        benign_sample_occurrences=benign_sample_occurrences,
    )

"""Canonical, deterministic family feature comparisons.

This module only classifies already extracted feature packs.  It never opens
sample bytes, runs extractors, or decides that two samples belong to a family.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import UUID

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SimilarityStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL_FEATURE_COVERAGE = "partial_feature_coverage"
    INSUFFICIENT_COMPARATIVE_SUPPORT = "insufficient_comparative_support"
    FEATURE_PACKS_UNAVAILABLE = "feature_packs_unavailable"
    NO_COMPARABLE_FEATURES = "no_comparable_features"


class SimilarityAvailabilityState(StrEnum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class SimilarityAvailabilityReason(StrEnum):
    SAMPLE_NOT_ACQUIRED = "sample_not_acquired"
    NOT_ASSOCIATED_TO_INVESTIGATION = "not_associated_to_investigation"
    FEATURE_PACK_MISSING = "feature_pack_missing"


class SimilarityAvailabilityNextAction(StrEnum):
    ACQUIRE_SAMPLE_IF_AUTHORIZED = "acquire_sample_if_authorized"
    VERIFY_INVESTIGATION_ASSOCIATION = "verify_investigation_association"
    VERIFY_ACQUISITION_OR_EXTRACT_FEATURES = "verify_acquisition_or_extract_features"


class FamilyFeatureClass(StrEnum):
    CORE_STRONGLY_SHARED = "core_strongly_shared"
    SUBFAMILY_DISCRIMINANT = "subfamily_discriminant"
    OPTIONAL = "optional"
    EXPLAINED_DIVERGENCE = "divergence_explained_by_version_or_packing"
    LIBRARY_OR_PACKER_CONTAMINATION = "library_or_packer_contamination"


class SimilarityFeatureKind(StrEnum):
    STRING = "string"
    IMPORT = "import"
    EXPORT = "export"
    SECTION = "section"
    PE_METADATA = "pe_metadata"
    ELF_METADATA = "elf_metadata"
    API = "api"
    CAPABILITY = "capability"
    CODE_NGRAM = "code_ngram"
    CONFIGURATION = "configuration"
    BEHAVIOR = "behavior"
    NETWORK = "network"
    EXACT_HASH = "exact_hash"
    IMPHASH = "imphash"
    TLSH_NEIGHBOR = "tlsh_neighbor"


class FeatureRejectionReason(StrEnum):
    INSUFFICIENT_POSITIVE_SUPPORT = "insufficient_positive_support"
    INSUFFICIENT_COMPARATIVE_SUPPORT = "insufficient_comparative_support"
    GOODWARE_PREVALENCE_UNKNOWN = "goodware_prevalence_unknown"
    HIGH_GOODWARE_PREVALENCE = "high_goodware_prevalence"
    HIGH_CORPUS_PREVALENCE = "high_corpus_prevalence"
    TOO_SHORT = "too_short"
    BOILERPLATE_OR_PADDING = "boilerplate_padding_or_stub"
    PACKER_ARTIFACT = "packer_artifact"
    LIBRARY_NOISE = "library_or_generic_api_noise"
    VERSION_OR_PACKING_DIVERGENCE = "version_or_packing_divergence"
    INSUFFICIENT_FIXED_BYTES = "insufficient_fixed_bytes"
    FAMILY_TRAIT_ONLY = "family_trait_not_a_signature_motif"
    EXACT_HASH_IDENTITY_ONLY = "exact_hash_is_identity_only"
    TLSH_NEIGHBOR_ONLY = "tlsh_is_neighbor_prescreen_only"
    SIMILARITY_HASH_ONLY = "similarity_hash_is_not_a_family_proof"


class GoodwarePrevalenceStatus(StrEnum):
    MEASURED = "measured"
    UNMEASURED = "unmeasured"


class SignatureMotifKind(StrEnum):
    ASCII = "ascii"
    WIDE = "wide"
    HEX = "hex"


@dataclass(frozen=True, slots=True, kw_only=True)
class CorpusMember:
    """One exact investigation-associated sample in a frozen corpus view."""

    sample_sha256: str
    qualification: str = "candidate"
    origin: str = "unknown"
    association_version: int = 1

    def __post_init__(self) -> None:
        _validate_sha256(self.sample_sha256, "sample_sha256")
        if not self.qualification.strip() or not self.origin.strip():
            raise ValueError("corpus member qualification and origin are required")
        if self.association_version < 1:
            raise ValueError("association_version must be positive")

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_sha256": self.sample_sha256,
            "qualification": self.qualification,
            "origin": self.origin,
            "association_version": self.association_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CorpusSnapshot:
    """Immutable investigation membership and goodware baseline identity."""

    investigation_id: UUID
    members: tuple[CorpusMember, ...]
    goodware_baseline_id: UUID | None = None
    goodware_baseline_sha256: str | None = None
    goodware_index_sha256: str | None = None

    def __post_init__(self) -> None:
        members = tuple(sorted(self.members, key=lambda item: item.sample_sha256))
        hashes = tuple(item.sample_sha256 for item in members)
        if len(hashes) != len(set(hashes)):
            raise ValueError("a corpus snapshot cannot contain duplicate sample hashes")
        if self.goodware_baseline_sha256 is not None:
            _validate_sha256(self.goodware_baseline_sha256, "goodware_baseline_sha256")
        if self.goodware_index_sha256 is not None:
            _validate_sha256(self.goodware_index_sha256, "goodware_index_sha256")
        if self.goodware_baseline_id is None and any(
            item is not None for item in (self.goodware_baseline_sha256, self.goodware_index_sha256)
        ):
            raise ValueError("goodware hashes require a goodware baseline id")
        object.__setattr__(self, "members", members)

    @property
    def sample_sha256s(self) -> tuple[str, ...]:
        return tuple(item.sample_sha256 for item in self.members)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": "corpus-snapshot-v1",
            "investigation_id": str(self.investigation_id),
            "members": [item.to_json() for item in self.members],
            "goodware_baseline_id": (
                str(self.goodware_baseline_id) if self.goodware_baseline_id is not None else None
            ),
            "goodware_baseline_sha256": self.goodware_baseline_sha256,
            "goodware_index_sha256": self.goodware_index_sha256,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_json())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class SimilarityPolicy:
    """Configurable support, noise, and resource thresholds for L2."""

    minimum_positive_support: int = 2
    minimum_string_characters: int = 8
    minimum_fixed_code_bytes: int = 8
    maximum_goodware_occurrences: int = 100
    maximum_corpus_prevalence: float = 0.8
    maximum_selected_samples: int = 32
    maximum_corpus_samples: int = 256
    maximum_features_per_sample: int = 5_000
    maximum_features: int = 25_000
    maximum_feature_value_characters: int = 2_048
    boilerplate_values: tuple[str, ...] = (
        "this program cannot be run in dos mode",
        "upx!",
        "upx0",
        "upx1",
        "upx2",
        "upx3",
        "themida",
        "vmprotect",
    )

    def __post_init__(self) -> None:
        if self.minimum_positive_support < 2:
            raise ValueError("minimum_positive_support must be at least 2")
        if self.minimum_string_characters < 1 or self.minimum_fixed_code_bytes < 1:
            raise ValueError("feature length thresholds must be positive")
        if self.maximum_goodware_occurrences < 0:
            raise ValueError("maximum_goodware_occurrences cannot be negative")
        if not 0.0 <= self.maximum_corpus_prevalence <= 1.0:
            raise ValueError("maximum_corpus_prevalence must be between 0 and 1")
        if (
            min(
                self.maximum_selected_samples,
                self.maximum_corpus_samples,
                self.maximum_features_per_sample,
                self.maximum_features,
                self.maximum_feature_value_characters,
            )
            < 1
        ):
            raise ValueError("similarity resource limits must be positive")
        object.__setattr__(
            self,
            "boilerplate_values",
            tuple(sorted({value.casefold() for value in self.boilerplate_values})),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "minimum_positive_support": self.minimum_positive_support,
            "minimum_string_characters": self.minimum_string_characters,
            "minimum_fixed_code_bytes": self.minimum_fixed_code_bytes,
            "maximum_goodware_occurrences": self.maximum_goodware_occurrences,
            "maximum_corpus_prevalence": self.maximum_corpus_prevalence,
            "maximum_selected_samples": self.maximum_selected_samples,
            "maximum_corpus_samples": self.maximum_corpus_samples,
            "maximum_features_per_sample": self.maximum_features_per_sample,
            "maximum_features": self.maximum_features,
            "maximum_feature_value_characters": self.maximum_feature_value_characters,
            "boilerplate_values": list(self.boilerplate_values),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureSource:
    source_kind: str
    source_id: str | None = None
    extractor_version: str | None = None
    parameters_sha256: str | None = None
    ruleset_sha256: str | None = None
    offset: int | None = None
    function_offset: int | None = None

    def __post_init__(self) -> None:
        if not self.source_kind.strip():
            raise ValueError("feature source kind is required")
        for name in ("offset", "function_offset"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in ("parameters_sha256", "ruleset_sha256"):
            value = getattr(self, name)
            if value is not None:
                _validate_sha256(value, name)

    def to_json(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "extractor_version": self.extractor_version,
            "parameters_sha256": self.parameters_sha256,
            "ruleset_sha256": self.ruleset_sha256,
            "offset": self.offset,
            "function_offset": self.function_offset,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleFeaturePresence:
    sample_sha256: str
    present: bool
    measured: bool
    selected: bool
    qualification: str
    offsets: tuple[int, ...] = ()
    function_offsets: tuple[int, ...] = ()
    sources: tuple[FeatureSource, ...] = ()

    def __post_init__(self) -> None:
        _validate_sha256(self.sample_sha256, "sample_sha256")
        object.__setattr__(self, "offsets", tuple(sorted(set(self.offsets))))
        object.__setattr__(self, "function_offsets", tuple(sorted(set(self.function_offsets))))
        object.__setattr__(self, "sources", tuple(sorted(self.sources, key=_source_sort_key)))
        if any(value < 0 for value in (*self.offsets, *self.function_offsets)):
            raise ValueError("feature offsets must be non-negative")
        if not self.present and (self.offsets or self.function_offsets or self.sources):
            raise ValueError("absent feature presence cannot include locations or sources")

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_sha256": self.sample_sha256,
            "present": self.present,
            "measured": self.measured,
            "selected": self.selected,
            "qualification": self.qualification,
            "offsets": list(self.offsets),
            "function_offsets": list(self.function_offsets),
            "sources": [item.to_json() for item in self.sources],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SignatureCandidate:
    feature_key: str
    motif_kind: SignatureMotifKind
    pattern: str
    qualified: bool
    positive_support: int
    positive_sample_sha256s: tuple[str, ...]
    source_offsets: tuple[SampleFeaturePresence, ...]
    rejection_reasons: tuple[FeatureRejectionReason, ...] = ()
    goodware_prevalence_status: GoodwarePrevalenceStatus = GoodwarePrevalenceStatus.UNMEASURED
    goodware_occurrence_count: int | None = None

    def __post_init__(self) -> None:
        _validate_sha256(self.feature_key, "feature_key")
        object.__setattr__(
            self, "positive_sample_sha256s", tuple(sorted(set(self.positive_sample_sha256s)))
        )
        object.__setattr__(
            self,
            "source_offsets",
            tuple(sorted(self.source_offsets, key=lambda x: x.sample_sha256)),
        )
        object.__setattr__(
            self, "rejection_reasons", tuple(sorted(set(self.rejection_reasons), key=str))
        )
        if self.goodware_prevalence_status is GoodwarePrevalenceStatus.MEASURED:
            if self.goodware_occurrence_count is None:
                raise ValueError("measured goodware prevalence requires an occurrence count")
        elif self.goodware_occurrence_count is not None:
            raise ValueError("unmeasured goodware prevalence cannot have an occurrence count")
        if self.goodware_occurrence_count is not None and self.goodware_occurrence_count < 0:
            raise ValueError("goodware occurrence count cannot be negative")
        if self.positive_support != len(self.positive_sample_sha256s):
            raise ValueError("positive_support must match positive_sample_sha256s")
        if self.qualified and self.rejection_reasons:
            raise ValueError("qualified signature candidates cannot have rejection reasons")

    def to_json(self) -> dict[str, Any]:
        return {
            "feature_key": self.feature_key,
            "motif_kind": self.motif_kind.value,
            "pattern": self.pattern,
            "qualified": self.qualified,
            "positive_support": self.positive_support,
            "positive_sample_sha256s": list(self.positive_sample_sha256s),
            "source_offsets": [item.to_json() for item in self.source_offsets],
            "rejection_reasons": [item.value for item in self.rejection_reasons],
            "goodware_prevalence_status": self.goodware_prevalence_status.value,
            "goodware_occurrence_count": self.goodware_occurrence_count,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureComparison:
    feature_key: str
    kind: SimilarityFeatureKind
    value: str
    encoding: str | None
    feature_class: FamilyFeatureClass
    rank: int
    positive_support: int
    positive_sample_count: int
    corpus_support: int
    corpus_sample_count: int
    goodware_occurrence_count: int | None
    comparison_exclusive: bool
    rejection_reasons: tuple[FeatureRejectionReason, ...]
    rationale: tuple[str, ...]
    sample_presence: tuple[SampleFeaturePresence, ...]
    signature_candidates: tuple[SignatureCandidate, ...] = ()

    def __post_init__(self) -> None:
        _validate_sha256(self.feature_key, "feature_key")
        if (
            min(self.rank, self.positive_support, self.positive_sample_count, self.corpus_support)
            < 0
        ):
            raise ValueError("feature rank and support counts cannot be negative")
        if self.positive_support > self.positive_sample_count:
            raise ValueError("positive support cannot exceed selected sample count")
        if self.corpus_support > self.corpus_sample_count:
            raise ValueError("corpus support cannot exceed corpus sample count")
        if self.goodware_occurrence_count is not None and self.goodware_occurrence_count < 0:
            raise ValueError("goodware occurrence count cannot be negative")
        object.__setattr__(
            self, "rejection_reasons", tuple(sorted(set(self.rejection_reasons), key=str))
        )
        object.__setattr__(self, "rationale", tuple(sorted(set(self.rationale))))
        object.__setattr__(
            self,
            "sample_presence",
            tuple(sorted(self.sample_presence, key=lambda x: x.sample_sha256)),
        )
        object.__setattr__(
            self,
            "signature_candidates",
            tuple(sorted(self.signature_candidates, key=lambda x: (x.motif_kind.value, x.pattern))),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "feature_key": self.feature_key,
            "kind": self.kind.value,
            "value": self.value,
            "encoding": self.encoding,
            "feature_class": self.feature_class.value,
            "rank": self.rank,
            "support": {
                "positive": self.positive_support,
                "positive_sample_count": self.positive_sample_count,
                "corpus": self.corpus_support,
                "corpus_sample_count": self.corpus_sample_count,
                "goodware_occurrence_count": self.goodware_occurrence_count,
                "goodware_prevalence_status": (
                    GoodwarePrevalenceStatus.MEASURED.value
                    if self.goodware_occurrence_count is not None
                    else GoodwarePrevalenceStatus.UNMEASURED.value
                ),
                "comparison_exclusive": self.comparison_exclusive,
            },
            "sample_presence": [item.to_json() for item in self.sample_presence],
            "rejection_reasons": [item.value for item in self.rejection_reasons],
            "rationale": list(self.rationale),
            "signature_candidates": [item.to_json() for item in self.signature_candidates],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractorVersion:
    sample_sha256: str
    pack_kind: str
    version: str
    parameters_sha256: str | None = None
    ruleset_sha256: str | None = None

    def __post_init__(self) -> None:
        _validate_sha256(self.sample_sha256, "sample_sha256")
        if not self.pack_kind.strip() or not self.version.strip():
            raise ValueError("extractor pack kind and version are required")
        if self.parameters_sha256 is not None:
            _validate_sha256(self.parameters_sha256, "parameters_sha256")
        if self.ruleset_sha256 is not None:
            _validate_sha256(self.ruleset_sha256, "ruleset_sha256")

    def to_json(self) -> dict[str, Any]:
        return {
            "sample_sha256": self.sample_sha256,
            "pack_kind": self.pack_kind,
            "version": self.version,
            "parameters_sha256": self.parameters_sha256,
            "ruleset_sha256": self.ruleset_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureLoss:
    cause: str
    count: int

    def __post_init__(self) -> None:
        if not self.cause.strip():
            raise ValueError("feature-loss cause is required")
        if self.count <= 0:
            raise ValueError("feature-loss count must be positive")

    def to_json(self) -> dict[str, Any]:
        return {"cause": self.cause, "count": self.count}


@dataclass(frozen=True, slots=True, kw_only=True)
class SimilarityLimits:
    selected_samples: int
    corpus_samples: int
    features: int
    packs_missing: tuple[str, ...] = ()
    corpus_sample_limit_applied: bool = False
    feature_limit_applied: bool = False
    per_sample_feature_limit_applied: tuple[str, ...] = ()
    feature_losses: tuple[FeatureLoss, ...] = ()

    def __post_init__(self) -> None:
        if min(self.selected_samples, self.corpus_samples, self.features) < 0:
            raise ValueError("similarity limits cannot be negative")
        object.__setattr__(self, "packs_missing", tuple(sorted(set(self.packs_missing))))
        object.__setattr__(
            self,
            "per_sample_feature_limit_applied",
            tuple(sorted(set(self.per_sample_feature_limit_applied))),
        )
        aggregated_losses: dict[str, int] = {}
        for loss in self.feature_losses:
            aggregated_losses[loss.cause] = aggregated_losses.get(loss.cause, 0) + loss.count
        object.__setattr__(
            self,
            "feature_losses",
            tuple(
                FeatureLoss(cause=cause, count=count)
                for cause, count in sorted(aggregated_losses.items())
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "selected_samples": self.selected_samples,
            "corpus_samples": self.corpus_samples,
            "features": self.features,
            "packs_missing": list(self.packs_missing),
            "corpus_sample_limit_applied": self.corpus_sample_limit_applied,
            "feature_limit_applied": self.feature_limit_applied,
            "per_sample_feature_limit_applied": list(self.per_sample_feature_limit_applied),
            "feature_losses": [item.to_json() for item in self.feature_losses],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class MeasuredFeature:
    """One value and provenance already present in an extractor feature pack."""

    kind: SimilarityFeatureKind
    value: str
    source: FeatureSource
    encoding: str | None = None
    category: str | None = None
    fixed_byte_count: int | None = None

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("measured feature value cannot be empty")
        if self.fixed_byte_count is not None and self.fixed_byte_count < 0:
            raise ValueError("fixed_byte_count cannot be negative")

    @property
    def key(self) -> str:
        return feature_key(self.kind, self.value, self.encoding)


@dataclass(frozen=True, slots=True, kw_only=True)
class SimilaritySampleInput:
    sample_sha256: str
    measured: bool
    features: tuple[MeasuredFeature, ...] = ()
    extractor_versions: tuple[ExtractorVersion, ...] = ()
    packer_markers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_sha256(self.sample_sha256, "sample_sha256")
        object.__setattr__(self, "features", tuple(sorted(self.features, key=_measured_sort_key)))
        object.__setattr__(
            self,
            "extractor_versions",
            tuple(sorted(self.extractor_versions, key=_extractor_sort_key)),
        )
        object.__setattr__(self, "packer_markers", tuple(sorted(set(self.packer_markers))))
        if not self.measured and (self.features or self.extractor_versions):
            raise ValueError("an unmeasured sample cannot include features or extractor versions")


def build_family_feature_comparison(
    *,
    snapshot: CorpusSnapshot,
    comparison_sample_sha256s: Sequence[str],
    sample_inputs: Sequence[SimilaritySampleInput],
    goodware_occurrences: Mapping[str, int],
    policy: SimilarityPolicy,
    limits: SimilarityLimits,
) -> FamilyFeatureComparisonV1:
    """Compare normalized feature pack values against exact positives and corpus."""
    selected = tuple(sorted(set(comparison_sample_sha256s)))
    if not selected or not set(selected).issubset(snapshot.sample_sha256s):
        raise ValueError("comparison samples must belong to the frozen corpus snapshot")
    input_by_hash = {item.sample_sha256: item for item in sample_inputs}
    if len(input_by_hash) != len(sample_inputs):
        raise ValueError("similarity inputs cannot contain duplicate samples")
    if not set(selected).issubset(input_by_hash):
        raise ValueError("every comparison sample requires a feature-pack result")

    sample_members = {member.sample_sha256: member for member in snapshot.members}
    occurrences: dict[str, dict[str, list[MeasuredFeature]]] = {}
    for sample_input in sample_inputs:
        for measured in sample_input.features:
            occurrences.setdefault(measured.key, {}).setdefault(
                sample_input.sample_sha256, []
            ).append(measured)

    features: list[FeatureComparison] = []
    loaded_hashes = {sample_hash for sample_hash, item in input_by_hash.items() if item.measured}
    selected_count = len(selected)
    for key, per_sample in occurrences.items():
        representative = min(
            (feature for sample_features in per_sample.values() for feature in sample_features),
            key=_measured_sort_key,
        )
        positive_hashes = tuple(
            sample_hash for sample_hash in selected if sample_hash in per_sample
        )
        corpus_hashes = tuple(
            sample_hash for sample_hash in snapshot.sample_sha256s if sample_hash in per_sample
        )
        context_loaded = tuple(
            sample_hash
            for sample_hash in snapshot.sample_sha256s
            if sample_hash not in selected and sample_hash in loaded_hashes
        )
        context_present = tuple(
            sample_hash for sample_hash in context_loaded if sample_hash in per_sample
        )
        absent_selected = tuple(
            sample_hash for sample_hash in selected if sample_hash not in per_sample
        )
        absence_coverage_complete = all(
            input_by_hash[sample_hash].measured for sample_hash in absent_selected
        )
        positive_support = len(positive_hashes)
        goodware_count = goodware_occurrences.get(key)
        reasons: set[FeatureRejectionReason] = set()
        rationale: set[str] = set()
        category_values = {
            (feature.category or "").casefold()
            for values in per_sample.values()
            for feature in values
        }
        is_packer_noise = bool(category_values & {"packer_artifact", "packer", "packing"})
        is_library_noise = bool(
            category_values & {"library_noise", "compiler_artifact", "generic_winapi", "library"}
        )
        if positive_support < policy.minimum_positive_support:
            reasons.add(FeatureRejectionReason.INSUFFICIENT_POSITIVE_SUPPORT)
            rationale.add("The feature is present in fewer than the configured positive samples.")
        if selected_count < 2:
            reasons.add(FeatureRejectionReason.INSUFFICIENT_COMPARATIVE_SUPPORT)
            rationale.add("One exact sample cannot establish comparative family support.")
        if goodware_count is None:
            reasons.add(FeatureRejectionReason.GOODWARE_PREVALENCE_UNKNOWN)
            rationale.add("No measured goodware occurrence count is available for this feature.")
        elif goodware_count > policy.maximum_goodware_occurrences:
            reasons.add(FeatureRejectionReason.HIGH_GOODWARE_PREVALENCE)
            rationale.add("Goodware prevalence exceeds the configured candidate threshold.")
        if (
            context_loaded
            and len(context_present) / len(context_loaded) >= policy.maximum_corpus_prevalence
        ):
            reasons.add(FeatureRejectionReason.HIGH_CORPUS_PREVALENCE)
            rationale.add("The feature is also common among other samples in this investigation.")
        if representative.kind is SimilarityFeatureKind.STRING:
            if len(representative.value.strip()) < policy.minimum_string_characters:
                reasons.add(FeatureRejectionReason.TOO_SHORT)
                rationale.add("Short strings are not promoted as signature candidates.")
            if representative.value.casefold().strip() in policy.boilerplate_values:
                reasons.add(FeatureRejectionReason.BOILERPLATE_OR_PADDING)
                rationale.add("The string matches configured boilerplate or packer markers.")
        elif representative.kind is SimilarityFeatureKind.CODE_NGRAM:
            fixed_bytes = representative.fixed_byte_count
            if fixed_bytes is None:
                fixed_bytes = _fixed_hex_bytes(representative.value)
            if fixed_bytes < policy.minimum_fixed_code_bytes:
                reasons.add(FeatureRejectionReason.INSUFFICIENT_FIXED_BYTES)
                rationale.add("The masked byte pattern has too few fixed bytes.")
            if _is_repeated_hex_pattern(representative.value):
                reasons.add(FeatureRejectionReason.BOILERPLATE_OR_PADDING)
                rationale.add("The byte pattern is repeated padding or a trivial instruction stub.")
        if representative.kind is SimilarityFeatureKind.EXACT_HASH:
            reasons.add(FeatureRejectionReason.EXACT_HASH_IDENTITY_ONLY)
            rationale.add("An exact hash identifies one file and cannot be a family signature.")
        elif representative.kind is SimilarityFeatureKind.IMPHASH:
            reasons.add(FeatureRejectionReason.SIMILARITY_HASH_ONLY)
            rationale.add(
                "Import hashes are similarity hints and are not family proof or YARA motifs."
            )
        elif representative.kind is SimilarityFeatureKind.TLSH_NEIGHBOR:
            reasons.add(FeatureRejectionReason.TLSH_NEIGHBOR_ONLY)
            rationale.add("TLSH is retained only as an optional neighbor prescreen hint.")
        if representative.kind in {
            SimilarityFeatureKind.CONFIGURATION,
            SimilarityFeatureKind.BEHAVIOR,
            SimilarityFeatureKind.NETWORK,
        }:
            reasons.add(FeatureRejectionReason.FAMILY_TRAIT_ONLY)
            rationale.add("This is a family-level trait and is not emitted as a YARA motif.")
        if is_packer_noise:
            reasons.add(FeatureRejectionReason.PACKER_ARTIFACT)
            rationale.add("The extractor marks this feature as a packer artifact.")
        if is_library_noise:
            reasons.add(FeatureRejectionReason.LIBRARY_NOISE)
            rationale.add("The extractor marks this feature as shared library or compiler noise.")

        versions_by_kind: dict[str, set[str]] = {}
        for sample_hash in selected:
            for version in input_by_hash[sample_hash].extractor_versions:
                versions_by_kind.setdefault(version.pack_kind, set()).add(version.version)
        packer_on_missing = any(
            sample_hash in absent_selected
            and input_by_hash.get(sample_hash) is not None
            and bool(input_by_hash[sample_hash].packer_markers)
            for sample_hash in absent_selected
        )
        version_divergence = any(len(versions) > 1 for versions in versions_by_kind.values())
        if absent_selected and not absence_coverage_complete:
            rationale.add("Feature absence is unknown because a selected feature pack is missing.")
        if (
            absent_selected
            and absence_coverage_complete
            and (version_divergence or packer_on_missing)
        ):
            reasons.add(FeatureRejectionReason.VERSION_OR_PACKING_DIVERGENCE)
            rationale.add("Absence may reflect extractor-version or packing differences.")

        contamination = bool(
            is_packer_noise
            or is_library_noise
            or FeatureRejectionReason.HIGH_GOODWARE_PREVALENCE in reasons
            or FeatureRejectionReason.HIGH_CORPUS_PREVALENCE in reasons
        )
        if contamination:
            feature_class = FamilyFeatureClass.LIBRARY_OR_PACKER_CONTAMINATION
        elif (
            absent_selected
            and absence_coverage_complete
            and FeatureRejectionReason.VERSION_OR_PACKING_DIVERGENCE in reasons
        ):
            feature_class = FamilyFeatureClass.EXPLAINED_DIVERGENCE
        elif (
            selected_count >= policy.minimum_positive_support
            and positive_support == selected_count
            and goodware_count is not None
            and goodware_count <= policy.maximum_goodware_occurrences
            and FeatureRejectionReason.BOILERPLATE_OR_PADDING not in reasons
            and FeatureRejectionReason.INSUFFICIENT_FIXED_BYTES not in reasons
            and FeatureRejectionReason.TOO_SHORT not in reasons
        ):
            feature_class = FamilyFeatureClass.CORE_STRONGLY_SHARED
        elif (
            positive_support >= policy.minimum_positive_support
            and positive_support < selected_count
            and absence_coverage_complete
        ):
            feature_class = FamilyFeatureClass.SUBFAMILY_DISCRIMINANT
        else:
            feature_class = FamilyFeatureClass.OPTIONAL

        candidate_reasons = set(reasons)
        if representative.kind in {
            SimilarityFeatureKind.CONFIGURATION,
            SimilarityFeatureKind.BEHAVIOR,
            SimilarityFeatureKind.NETWORK,
            SimilarityFeatureKind.EXACT_HASH,
            SimilarityFeatureKind.IMPHASH,
            SimilarityFeatureKind.TLSH_NEIGHBOR,
        }:
            candidate_reasons.add(FeatureRejectionReason.FAMILY_TRAIT_ONLY)
        candidate_qualified = (
            not candidate_reasons
            and positive_support >= policy.minimum_positive_support
            and goodware_count is not None
        )
        presence: list[SampleFeaturePresence] = []
        for sample_hash in snapshot.sample_sha256s:
            sample = input_by_hash.get(sample_hash)
            sample_features = per_sample.get(sample_hash, ())
            member = sample_members[sample_hash]
            sources = tuple(feature.source for feature in sample_features)
            presence.append(
                SampleFeaturePresence(
                    sample_sha256=sample_hash,
                    present=bool(sample_features),
                    measured=sample is not None and sample.measured,
                    selected=sample_hash in selected,
                    qualification=member.qualification,
                    offsets=tuple(
                        feature.source.offset
                        for feature in sample_features
                        if feature.source.offset is not None
                    ),
                    function_offsets=tuple(
                        feature.source.function_offset
                        for feature in sample_features
                        if feature.source.function_offset is not None
                    ),
                    sources=sources,
                )
            )
        candidates = _signature_candidates(
            representative=representative,
            key=key,
            positive_hashes=positive_hashes,
            positive_support=positive_support,
            presence=presence,
            qualified=candidate_qualified,
            rejection_reasons=tuple(candidate_reasons),
            policy=policy,
            goodware_occurrence_count=goodware_count,
        )
        features.append(
            FeatureComparison(
                feature_key=key,
                kind=representative.kind,
                value=representative.value,
                encoding=representative.encoding,
                feature_class=feature_class,
                rank=0,
                positive_support=positive_support,
                positive_sample_count=selected_count,
                corpus_support=len(corpus_hashes),
                corpus_sample_count=len(snapshot.members),
                goodware_occurrence_count=goodware_count,
                comparison_exclusive=(
                    len(context_loaded)
                    == len(
                        tuple(
                            sample_hash
                            for sample_hash in snapshot.sample_sha256s
                            if sample_hash not in selected
                        )
                    )
                    and not context_present
                ),
                rejection_reasons=tuple(reasons),
                rationale=tuple(rationale),
                sample_presence=tuple(presence),
                signature_candidates=candidates,
            )
        )

    features.sort(key=_feature_rank_key)
    ranked = tuple(replace(item, rank=index) for index, item in enumerate(features, start=1))
    all_selected_measured = all(
        sample_hash in input_by_hash and input_by_hash[sample_hash].measured
        for sample_hash in selected
    )
    measured_selected_count = sum(
        sample_hash in input_by_hash and input_by_hash[sample_hash].measured
        for sample_hash in selected
    )
    if not all_selected_measured:
        status = (
            SimilarityStatus.FEATURE_PACKS_UNAVAILABLE
            if measured_selected_count == 0
            else SimilarityStatus.PARTIAL_FEATURE_COVERAGE
        )
    elif len(selected) == 1:
        status = SimilarityStatus.INSUFFICIENT_COMPARATIVE_SUPPORT
    elif not ranked:
        status = SimilarityStatus.NO_COMPARABLE_FEATURES
    elif (
        limits.packs_missing
        or limits.corpus_sample_limit_applied
        or limits.feature_limit_applied
        or limits.per_sample_feature_limit_applied
        or limits.feature_losses
    ):
        status = SimilarityStatus.PARTIAL_FEATURE_COVERAGE
    else:
        status = SimilarityStatus.COMPLETE

    warnings = {
        f"packer markers observed for {sample.sample_sha256}: {', '.join(sample.packer_markers)}"
        for sample in sample_inputs
        if sample.packer_markers
    }
    warnings.add("Similarity and TLSH hints do not establish family membership.")
    if any(item.kind is SimilarityFeatureKind.TLSH_NEIGHBOR for item in ranked):
        warnings.add("TLSH values were compared exactly; no fuzzy distance was calculated.")
    next_tests: set[str] = set()
    if len(selected) == 1:
        next_tests.add(
            "Compare at least one additional exact SHA-256 sample from the proposed family."
        )
    if any(item.goodware_occurrence_count is None for item in ranked):
        next_tests.add(
            "Measure candidate goodware prevalence through the RuleWorkbench/goodware "
            "workflow in L3 before qualifying its signature motif."
        )
    if any(item.feature_class is FamilyFeatureClass.EXPLAINED_DIVERGENCE for item in ranked):
        next_tests.add(
            "Re-extract divergent samples with matching extractor versions and inspect "
            "packing differences."
        )
    if limits.packs_missing:
        next_tests.add(
            "Make the missing persisted feature packs available and rerun this comparison."
        )

    return FamilyFeatureComparisonV1(
        investigation_id=snapshot.investigation_id,
        corpus_snapshot_sha256=snapshot.sha256,
        comparison_sample_sha256s=selected,
        corpus_sample_sha256s=snapshot.sample_sha256s,
        status=status,
        policy=policy,
        limits=limits,
        extractor_versions=tuple(
            version for sample in sample_inputs for version in sample.extractor_versions
        ),
        features=ranked,
        recommended_next_tests=tuple(next_tests),
        warnings=tuple(warnings),
    )


def _signature_candidates(
    *,
    representative: MeasuredFeature,
    key: str,
    positive_hashes: tuple[str, ...],
    positive_support: int,
    presence: Sequence[SampleFeaturePresence],
    qualified: bool,
    rejection_reasons: tuple[FeatureRejectionReason, ...],
    policy: SimilarityPolicy,
    goodware_occurrence_count: int | None,
) -> tuple[SignatureCandidate, ...]:
    excluded_reasons = {
        FeatureRejectionReason.TOO_SHORT,
        FeatureRejectionReason.BOILERPLATE_OR_PADDING,
        FeatureRejectionReason.HIGH_GOODWARE_PREVALENCE,
        FeatureRejectionReason.HIGH_CORPUS_PREVALENCE,
        FeatureRejectionReason.PACKER_ARTIFACT,
        FeatureRejectionReason.LIBRARY_NOISE,
        FeatureRejectionReason.INSUFFICIENT_FIXED_BYTES,
    }
    if excluded_reasons.intersection(rejection_reasons):
        return ()
    if representative.kind is SimilarityFeatureKind.CODE_NGRAM:
        motif_kinds = (SignatureMotifKind.HEX,)
    elif representative.kind in {
        SimilarityFeatureKind.STRING,
        SimilarityFeatureKind.IMPORT,
        SimilarityFeatureKind.EXPORT,
        SimilarityFeatureKind.SECTION,
        SimilarityFeatureKind.API,
        SimilarityFeatureKind.CAPABILITY,
        SimilarityFeatureKind.PE_METADATA,
        SimilarityFeatureKind.ELF_METADATA,
    }:
        if len(representative.value.strip()) < policy.minimum_string_characters:
            return ()
        if representative.encoding in {"utf-16le", "wide", "utf16le"}:
            motif_kinds = (SignatureMotifKind.WIDE,)
        else:
            motif_kinds = (SignatureMotifKind.ASCII,)
    else:
        return ()
    evidence = tuple(item for item in presence if item.present and item.selected)
    return tuple(
        SignatureCandidate(
            feature_key=key,
            motif_kind=motif_kind,
            pattern=representative.value,
            qualified=qualified,
            positive_support=positive_support,
            positive_sample_sha256s=positive_hashes,
            source_offsets=evidence,
            rejection_reasons=rejection_reasons,
            goodware_prevalence_status=(
                GoodwarePrevalenceStatus.MEASURED
                if goodware_occurrence_count is not None
                else GoodwarePrevalenceStatus.UNMEASURED
            ),
            goodware_occurrence_count=goodware_occurrence_count,
        )
        for motif_kind in motif_kinds
    )


def _fixed_hex_bytes(pattern: str) -> int:
    return sum(token not in {"?", "??", "*"} for token in pattern.split())


def _is_repeated_hex_pattern(pattern: str) -> bool:
    tokens = [token.lower() for token in pattern.split() if token not in {"?", "??", "*"}]
    return len(tokens) >= 4 and len(set(tokens)) == 1


def _measured_sort_key(item: MeasuredFeature) -> tuple[str, ...]:
    return (
        item.kind.value,
        item.value,
        item.encoding or "",
        item.category or "",
        item.source.source_kind,
        item.source.source_id or "",
        str(item.source.offset if item.source.offset is not None else -1),
        str(item.source.function_offset if item.source.function_offset is not None else -1),
    )


def _feature_rank_key(item: FeatureComparison) -> tuple[Any, ...]:
    class_order = {
        FamilyFeatureClass.CORE_STRONGLY_SHARED: 0,
        FamilyFeatureClass.SUBFAMILY_DISCRIMINANT: 1,
        FamilyFeatureClass.EXPLAINED_DIVERGENCE: 2,
        FamilyFeatureClass.OPTIONAL: 3,
        FamilyFeatureClass.LIBRARY_OR_PACKER_CONTAMINATION: 4,
    }
    prevalence = item.goodware_occurrence_count
    return (
        class_order[item.feature_class],
        -item.positive_support,
        prevalence if prevalence is not None else 2**63,
        item.kind.value,
        item.value,
        item.feature_key,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FamilyFeatureComparisonV1:
    investigation_id: UUID
    corpus_snapshot_sha256: str
    comparison_sample_sha256s: tuple[str, ...]
    corpus_sample_sha256s: tuple[str, ...]
    status: SimilarityStatus
    policy: SimilarityPolicy
    limits: SimilarityLimits
    extractor_versions: tuple[ExtractorVersion, ...]
    features: tuple[FeatureComparison, ...]
    recommended_next_tests: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    schema_version: str = "family-feature-comparison-v1"

    def __post_init__(self) -> None:
        _validate_sha256(self.corpus_snapshot_sha256, "corpus_snapshot_sha256")
        selected = tuple(sorted(set(self.comparison_sample_sha256s)))
        corpus = tuple(sorted(set(self.corpus_sample_sha256s)))
        if not selected or not set(selected).issubset(corpus):
            raise ValueError("comparison samples must be a non-empty subset of the corpus snapshot")
        for sample_hash in (*selected, *corpus):
            _validate_sha256(sample_hash, "sample_sha256")
        object.__setattr__(self, "comparison_sample_sha256s", selected)
        object.__setattr__(self, "corpus_sample_sha256s", corpus)
        object.__setattr__(
            self,
            "extractor_versions",
            tuple(sorted(self.extractor_versions, key=_extractor_sort_key)),
        )
        object.__setattr__(
            self, "features", tuple(sorted(self.features, key=lambda item: item.rank))
        )
        object.__setattr__(
            self, "recommended_next_tests", tuple(sorted(set(self.recommended_next_tests)))
        )
        object.__setattr__(self, "warnings", tuple(sorted(set(self.warnings))))

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "investigation_id": str(self.investigation_id),
            "corpus_snapshot_sha256": self.corpus_snapshot_sha256,
            "comparison_sample_sha256s": list(self.comparison_sample_sha256s),
            "corpus_sample_sha256s": list(self.corpus_sample_sha256s),
            "status": self.status.value,
            "policy": self.policy.to_json(),
            "limits": self.limits.to_json(),
            "extractor_versions": [item.to_json() for item in self.extractor_versions],
            "features": [item.to_json() for item in self.features],
            "recommended_next_tests": list(self.recommended_next_tests),
            "warnings": list(self.warnings),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_json())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def feature(self, feature_key: str) -> FeatureComparison | None:
        return next((item for item in self.features if item.feature_key == feature_key), None)


def feature_key(kind: SimilarityFeatureKind, value: str, encoding: str | None = None) -> str:
    """Return a stable key for one normalized measured feature."""
    return hashlib.sha256(
        _canonical_json_bytes({"kind": kind.value, "value": value, "encoding": encoding})
    ).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """Public canonical JSON encoder used by the application blob adapter."""
    return _canonical_json_bytes(value)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256")


def _source_sort_key(source: FeatureSource) -> tuple[str, ...]:
    return (
        source.source_kind,
        source.source_id or "",
        source.extractor_version or "",
        source.parameters_sha256 or "",
        source.ruleset_sha256 or "",
        str(source.offset if source.offset is not None else -1),
        str(source.function_offset if source.function_offset is not None else -1),
    )


def _extractor_sort_key(item: ExtractorVersion) -> tuple[str, ...]:
    return (
        item.sample_sha256,
        item.pack_kind,
        item.version,
        item.parameters_sha256 or "",
        item.ruleset_sha256 or "",
    )


__all__ = [
    "CorpusMember",
    "CorpusSnapshot",
    "ExtractorVersion",
    "FamilyFeatureClass",
    "FamilyFeatureComparisonV1",
    "FeatureComparison",
    "FeatureLoss",
    "FeatureRejectionReason",
    "FeatureSource",
    "GoodwarePrevalenceStatus",
    "MeasuredFeature",
    "SampleFeaturePresence",
    "SignatureCandidate",
    "SignatureMotifKind",
    "SimilarityAvailabilityNextAction",
    "SimilarityAvailabilityReason",
    "SimilarityAvailabilityState",
    "SimilarityFeatureKind",
    "SimilarityLimits",
    "SimilarityPolicy",
    "SimilaritySampleInput",
    "SimilarityStatus",
    "build_family_feature_comparison",
    "canonical_json_bytes",
    "feature_key",
]

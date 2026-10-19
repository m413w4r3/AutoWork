from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4


class RuleMotifKind(StrEnum):
    STRING = "string"
    HEX = "hex"
    WIDE = "wide"
    BASE64 = "base64"
    CODE_SEQUENCE = "code_sequence"


class RuleMotifProvenanceKind(StrEnum):
    P09_INVARIANT = "p09_invariant"
    P10_PROPOSAL = "p10_proposal"
    BRIDGE = "bridge"
    MANUAL = "manual"


class RuleTestCorpusRole(StrEnum):
    CONFIRMED_POSITIVE = "confirmed_positive"
    OUT_OF_CLUSTER_CANDIDATE = "out_of_cluster_candidate"
    GOODWARE = "goodware"
    HOLDOUT = "holdout"


class RuleSampleProvenance(StrEnum):
    REAL = "real"
    SYNTHETIC = "synthetic"


class RuleValidationStatus(StrEnum):
    UNTESTED = "untested"
    SYNTHETIC_ONLY = "synthetic_only"
    REAL_PARTIAL = "real_partial"
    REAL_VALIDATED = "real_validated"


class RuleTestMeasurementMethod(StrEnum):
    ISOLATED_YARA_WORKER = "isolated_yara_worker"
    CONFINED_ANALYSIS_WORKER = "confined_analysis_worker"


class RuleConditionStrategy(StrEnum):
    ANY = "any"
    ALL = "all"
    MAJORITY = "majority"


class RuleMotifIssueCode(StrEnum):
    EMPTY = "empty"
    TOO_SHORT = "too_short"
    LOW_ENTROPY = "low_entropy"
    TRIVIAL = "trivial"
    KNOWN_NON_DISCRIMINANT = "known_non_discriminant"
    INVALID_HEX = "invalid_hex"
    TOO_MANY_MOTIFS = "too_many_motifs"
    INVALID_METADATA = "invalid_metadata"
    MISSING_OBSERVATION = "missing_observation"
    GENERIC_DOTNET_IMPORT = "generic_dotnet_import"
    SOURCE_BYTES_UNVERIFIED = "source_bytes_unverified"


class YaraExecutionStatus(StrEnum):
    OK = "ok"
    COMPILE_ERROR = "compile_error"
    TIMEOUT = "timeout"
    RESOURCE_EXCEEDED = "resource_exceeded"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    ENGINE_CRASHED = "engine_crashed"
    INVALID_INPUT = "invalid_input"


class RuleTestErrorCode(StrEnum):
    COMPILE_ERROR = "compile_error"
    TIMEOUT = "timeout"
    RESOURCE_EXCEEDED = "resource_exceeded"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    ENGINE_CRASHED = "engine_crashed"
    CORPUS_ERROR = "corpus_error"
    INVALID_SAMPLE = "invalid_sample"


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleMotif:
    kind: RuleMotifKind
    value: str
    provenance_kind: RuleMotifProvenanceKind
    provenance_id: str
    observation_refs: tuple[UUID, ...] = ()
    article_claim_refs: tuple[str, ...] = ()
    sample_sha256: str | None = None
    offsets: tuple[int, ...] | None = None
    rationale: str = ""
    positive_support: int | None = None
    benign_prevalence: int | None = None
    goodware_occurrence_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RuleMotifKind(self.kind))
        object.__setattr__(self, "provenance_kind", RuleMotifProvenanceKind(self.provenance_kind))
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("motif value cannot be empty")
        if not isinstance(self.provenance_id, str) or not self.provenance_id.strip():
            raise ValueError("motif provenance_id cannot be empty")
        if self.sample_sha256 is not None:
            _require_sha256(self.sample_sha256, "sample_sha256")
        if self.offsets is not None:
            offsets = tuple(self.offsets)
            if not offsets or any(not isinstance(offset, int) or offset < 0 for offset in offsets):
                raise ValueError("motif offsets must be non-negative measured offsets")
            if self.sample_sha256 is None:
                raise ValueError("measured offsets require a sample SHA-256")
            object.__setattr__(self, "offsets", offsets)
        refs = tuple(self.observation_refs)
        if any(not isinstance(reference, UUID) for reference in refs):
            raise TypeError("observation_refs must contain UUID values")
        if len(refs) != len(set(refs)):
            raise ValueError("observation_refs must be unique")
        object.__setattr__(self, "observation_refs", refs)
        claim_refs = tuple(self.article_claim_refs)
        if any(
            not isinstance(reference, str) or not reference.strip() or len(reference) > 128
            for reference in claim_refs
        ):
            raise ValueError("article_claim_refs must contain bounded claim ids")
        if len(claim_refs) != len(set(claim_refs)):
            raise ValueError("article_claim_refs must be unique")
        object.__setattr__(self, "article_claim_refs", claim_refs)
        for name, value in (
            ("positive_support", self.positive_support),
            ("benign_prevalence", self.benign_prevalence),
            ("goodware_occurrence_count", self.goodware_occurrence_count),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleTestCase:
    sample_sha256: str
    role: RuleTestCorpusRole
    provenance: RuleSampleProvenance = RuleSampleProvenance.REAL
    sample_id: UUID | None = None
    expected_match: bool | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256, "sample_sha256")
        object.__setattr__(self, "role", RuleTestCorpusRole(self.role))
        object.__setattr__(self, "provenance", RuleSampleProvenance(self.provenance))
        if self.expected_match is not None and not isinstance(self.expected_match, bool):
            raise TypeError("expected_match must be a boolean or None")

    @property
    def expected_result(self) -> bool | None:
        if self.expected_match is not None:
            return self.expected_match
        if self.role is RuleTestCorpusRole.CONFIRMED_POSITIVE:
            return True
        if self.role is RuleTestCorpusRole.GOODWARE:
            return False
        return None


def rule_corpus_sha256(cases: Sequence[RuleTestCase]) -> str:
    """Hash sample identities together with role, provenance, and declared truth labels."""
    normalized = sorted(cases, key=lambda case: (case.role.value, case.sample_sha256))
    payload = "\n".join(
        ":".join(
            (
                case.role.value,
                case.sample_sha256,
                case.provenance.value,
                "unknown" if case.expected_result is None else str(case.expected_result).lower(),
            )
        )
        for case in normalized
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleMotifIssue:
    code: RuleMotifIssueCode
    message: str
    motif_index: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleMotifValidationReport:
    issues: tuple[RuleMotifIssue, ...] = ()
    verified_motif_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        indices = tuple(self.verified_motif_indices)
        if any(
            not isinstance(index, int) or isinstance(index, bool) or index < 0 for index in indices
        ):
            raise ValueError("verified motif indices must be non-negative integers")
        if len(indices) != len(set(indices)):
            raise ValueError("verified motif indices must be unique")
        object.__setattr__(self, "verified_motif_indices", tuple(sorted(indices)))

    @property
    def valid(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleBuildResult:
    source: str | None
    rule_identifier: str | None
    validation: RuleMotifValidationReport

    @property
    def valid(self) -> bool:
        return self.source is not None and self.validation.valid

    @property
    def accepted(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleTestError:
    code: RuleTestErrorCode
    message: str
    sample_sha256: str | None = None
    line: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", RuleTestErrorCode(self.code))
        object.__setattr__(self, "message", self.message[:1000])
        if self.sample_sha256 is not None:
            _require_sha256(self.sample_sha256, "sample_sha256")
        if self.line is not None and self.line < 1:
            raise ValueError("line must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleRoleTestReport:
    role: RuleTestCorpusRole
    corpus_sha256s: tuple[str, ...]
    test_cases: tuple[RuleTestCase, ...] = ()
    matches: tuple[str, ...] = ()
    non_matches: tuple[str, ...] = ()
    non_measurable: tuple[str, ...] = ()
    errors: tuple[RuleTestError, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", RuleTestCorpusRole(self.role))
        for field_name in ("corpus_sha256s", "matches", "non_matches", "non_measurable"):
            values = tuple(getattr(self, field_name))
            for value in values:
                _require_sha256(value, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must contain unique SHA-256 values")
            object.__setattr__(self, field_name, values)
        test_cases = tuple(self.test_cases)
        if any(case.role is not self.role for case in test_cases):
            raise ValueError("test case role does not match its role report")
        if len({case.sample_sha256 for case in test_cases}) != len(test_cases):
            raise ValueError("role report test cases must be unique by SHA-256")
        if {case.sample_sha256 for case in test_cases} != set(self.corpus_sha256s):
            if self.corpus_sha256s:
                raise ValueError("role report test cases must match its declared corpus")
        object.__setattr__(self, "test_cases", test_cases)
        measured = set(self.matches) | set(self.non_matches)
        if set(self.matches) & set(self.non_matches):
            raise ValueError("a corpus member cannot both match and not match")
        if measured & set(self.non_measurable):
            raise ValueError("a corpus member cannot be measured and non-measurable")
        error_hashes = {error.sample_sha256 for error in self.errors if error.sample_sha256}
        if not error_hashes.issubset(set(self.corpus_sha256s)):
            raise ValueError("test errors must belong to the declared corpus")
        outcomes = measured | set(self.non_measurable) | error_hashes
        if outcomes != set(self.corpus_sha256s):
            raise ValueError("every corpus member must have exactly one test result")
        if not measured.issubset(set(self.corpus_sha256s)) or not set(self.non_measurable).issubset(
            set(self.corpus_sha256s)
        ):
            raise ValueError("test results must belong to the declared corpus")

    @property
    def provenance_counts(self) -> dict[str, int]:
        counts = {item.value: 0 for item in RuleSampleProvenance}
        for case in self.test_cases:
            counts[case.provenance.value] += 1
        return counts


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleTestReport:
    source_sha256: str
    execution_status: YaraExecutionStatus
    engine_version: str
    roles: tuple[RuleRoleTestReport, ...]
    selection_bias: str
    coverage_statement: str
    measurement_method: RuleTestMeasurementMethod = RuleTestMeasurementMethod.ISOLATED_YARA_WORKER
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_sha256(self.source_sha256, "source_sha256")
        object.__setattr__(self, "execution_status", YaraExecutionStatus(self.execution_status))
        object.__setattr__(
            self, "measurement_method", RuleTestMeasurementMethod(self.measurement_method)
        )
        roles = tuple(self.roles)
        if {result.role for result in roles} != set(RuleTestCorpusRole) or len(roles) != len(
            RuleTestCorpusRole
        ):
            raise ValueError("a report must include exactly one result for each corpus role")
        object.__setattr__(
            self, "roles", tuple(sorted(roles, key=lambda result: result.role.value))
        )
        if not self.engine_version.strip():
            raise ValueError("engine_version must be declared")
        if not self.selection_bias.strip() or not self.coverage_statement.strip():
            raise ValueError("selection bias and coverage must be declared")

    @property
    def corpus_sha256(self) -> str:
        return rule_corpus_sha256(tuple(case for role in self.roles for case in role.test_cases))

    @property
    def provenance_counts_by_role(self) -> dict[str, dict[str, int]]:
        return {result.role.value: result.provenance_counts for result in self.roles}

    @property
    def false_positive_sha256s(self) -> tuple[str, ...]:
        goodware = next(item for item in self.roles if item.role is RuleTestCorpusRole.GOODWARE)
        return tuple(sorted(goodware.matches))

    @property
    def false_negative_sha256s(self) -> tuple[str, ...]:
        positives = next(
            item for item in self.roles if item.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
        )
        return tuple(sorted(positives.non_matches))

    @property
    def metrics(self) -> dict[str, dict[str, object]]:
        """Rates on real, measured samples; unknown labels never enter denominators."""
        positives = next(
            item for item in self.roles if item.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
        )
        goodware = next(item for item in self.roles if item.role is RuleTestCorpusRole.GOODWARE)
        holdout = next(item for item in self.roles if item.role is RuleTestCorpusRole.HOLDOUT)
        real_positive_hashes = {
            case.sample_sha256
            for case in positives.test_cases
            if case.provenance is RuleSampleProvenance.REAL
            and case.sample_sha256 in set(positives.matches) | set(positives.non_matches)
        }
        real_goodware_hashes = {
            case.sample_sha256
            for case in goodware.test_cases
            if case.provenance is RuleSampleProvenance.REAL
            and case.sample_sha256 in set(goodware.matches) | set(goodware.non_matches)
        }
        measured_holdout_hashes = set(holdout.matches) | set(holdout.non_matches)
        real_holdout_positives = {
            case.sample_sha256
            for case in holdout.test_cases
            if case.provenance is RuleSampleProvenance.REAL
            and case.expected_result is True
            and case.sample_sha256 in measured_holdout_hashes
        }
        real_holdout_negatives = {
            case.sample_sha256
            for case in holdout.test_cases
            if case.provenance is RuleSampleProvenance.REAL
            and case.expected_result is False
            and case.sample_sha256 in measured_holdout_hashes
        }
        return {
            "real_positive_sensitivity": wilson_rate(
                successes=len(real_positive_hashes & set(positives.matches)),
                trials=len(real_positive_hashes),
            ),
            "real_goodware_false_positive_rate": wilson_rate(
                successes=len(real_goodware_hashes & set(goodware.matches)),
                trials=len(real_goodware_hashes),
            ),
            "real_holdout_sensitivity": wilson_rate(
                successes=len(real_holdout_positives & set(holdout.matches)),
                trials=len(real_holdout_positives),
            ),
            "real_holdout_false_positive_rate": wilson_rate(
                successes=len(real_holdout_negatives & set(holdout.matches)),
                trials=len(real_holdout_negatives),
            ),
        }

    @property
    def validation_status(self) -> RuleValidationStatus:
        measured_roles = {
            role.role: set(role.matches) | set(role.non_matches) for role in self.roles
        }
        measured_cases = [
            case
            for role in self.roles
            for case in role.test_cases
            if case.sample_sha256 in measured_roles[role.role]
        ]
        if not measured_cases:
            return RuleValidationStatus.UNTESTED

        positives = next(
            item for item in self.roles if item.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
        )
        goodware = next(item for item in self.roles if item.role is RuleTestCorpusRole.GOODWARE)
        measured_real_positives = [
            case
            for case in positives.test_cases
            if case.provenance is RuleSampleProvenance.REAL
            and case.sample_sha256 in measured_roles[RuleTestCorpusRole.CONFIRMED_POSITIVE]
        ]
        measured_real_goodware = [
            case
            for case in goodware.test_cases
            if case.provenance is RuleSampleProvenance.REAL
            and case.sample_sha256 in measured_roles[RuleTestCorpusRole.GOODWARE]
        ]
        if not measured_real_positives:
            if any(case.provenance is RuleSampleProvenance.REAL for case in measured_cases):
                return RuleValidationStatus.REAL_PARTIAL
            return RuleValidationStatus.SYNTHETIC_ONLY

        if (
            self.execution_status is YaraExecutionStatus.OK
            and measured_real_goodware
            and len(measured_real_positives)
            == sum(case.provenance is RuleSampleProvenance.REAL for case in positives.test_cases)
            and len(measured_real_goodware)
            == sum(case.provenance is RuleSampleProvenance.REAL for case in goodware.test_cases)
            and all(case.sample_sha256 in positives.matches for case in measured_real_positives)
            and not any(case.sample_sha256 in goodware.matches for case in measured_real_goodware)
            and not positives.errors
            and not goodware.errors
            and not any(
                case.provenance is RuleSampleProvenance.REAL
                for case in positives.test_cases
                if case.sample_sha256 in positives.non_measurable
            )
            and not any(
                case.provenance is RuleSampleProvenance.REAL
                for case in goodware.test_cases
                if case.sample_sha256 in goodware.non_measurable
            )
        ):
            return RuleValidationStatus.REAL_VALIDATED
        return RuleValidationStatus.REAL_PARTIAL


def wilson_rate(*, successes: int, trials: int) -> dict[str, object]:
    if trials == 0:
        return {
            "status": "not_calculable",
            "numerator": 0,
            "denominator": 0,
            "estimate": None,
            "confidence_interval_95": None,
            "method": "wilson_score_two_sided_95",
        }
    if not 0 <= successes <= trials:
        raise ValueError("rate numerator must be between zero and the denominator")
    z = 1.959963984540054
    estimate = successes / trials
    denominator = 1 + z * z / trials
    center = (estimate + z * z / (2 * trials)) / denominator
    half_width = (
        z
        * ((estimate * (1 - estimate) / trials + z * z / (4 * trials * trials)) ** 0.5)
        / denominator
    )
    lower = max(0.0, center - half_width)
    upper = min(1.0, center + half_width)
    if successes == 0:
        lower = 0.0
    if successes == trials:
        upper = 1.0
    return {
        "status": "calculated",
        "numerator": successes,
        "denominator": trials,
        "estimate": estimate,
        "confidence_interval_95": {
            "lower": lower,
            "upper": upper,
        },
        "method": "wilson_score_two_sided_95",
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleCorpusInventoryItem:
    sample_sha256: str
    role: RuleTestCorpusRole
    provenance: RuleSampleProvenance
    size_bytes: int
    source_description: str = ""

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256, "sample_sha256")
        object.__setattr__(self, "role", RuleTestCorpusRole(self.role))
        object.__setattr__(self, "provenance", RuleSampleProvenance(self.provenance))
        if self.role is RuleTestCorpusRole.HOLDOUT:
            raise ValueError("operator imports cannot declare the holdout role")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")
        if not isinstance(self.source_description, str):
            raise TypeError("source_description must be text")
        if len(self.source_description) > 2_000:
            raise ValueError("source_description is too long")


@dataclass(frozen=True, slots=True, kw_only=True)
class YaraEngineResult:
    status: YaraExecutionStatus
    engine_version: str
    matches_sha256: tuple[str, ...] = ()
    message: str = ""
    line: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", YaraExecutionStatus(self.status))
        object.__setattr__(self, "message", self.message[:1000])
        matches = tuple(self.matches_sha256)
        for sample_sha256 in matches:
            _require_sha256(sample_sha256, "matches_sha256")
        object.__setattr__(self, "matches_sha256", matches)
        if self.line is not None and self.line < 1:
            raise ValueError("line must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleRoleVersionComparison:
    role: RuleTestCorpusRole
    matches_gained: tuple[str, ...] = ()
    matches_lost: tuple[str, ...] = ()
    non_comparable: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", RuleTestCorpusRole(self.role))
        for field_name in ("matches_gained", "matches_lost", "non_comparable"):
            values = tuple(getattr(self, field_name))
            for value in values:
                _require_sha256(value, field_name)
            object.__setattr__(self, field_name, values)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleVersionComparison:
    version_a_id: UUID
    version_b_id: UUID
    version_a: str
    version_b: str
    roles: tuple[RuleRoleVersionComparison, ...]
    false_positives_added: tuple[str, ...] = ()
    false_positives_removed: tuple[str, ...] = ()
    engine_version_a: str = "unknown"
    engine_version_b: str = "unknown"
    metrics_a: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    metrics_b: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    measurement_method_a: RuleTestMeasurementMethod = RuleTestMeasurementMethod.ISOLATED_YARA_WORKER
    measurement_method_b: RuleTestMeasurementMethod = RuleTestMeasurementMethod.ISOLATED_YARA_WORKER

    def __post_init__(self) -> None:
        roles = tuple(self.roles)
        if {result.role for result in roles} != set(RuleTestCorpusRole) or len(roles) != len(
            RuleTestCorpusRole
        ):
            raise ValueError("a version comparison must include every corpus role")
        object.__setattr__(
            self, "roles", tuple(sorted(roles, key=lambda result: result.role.value))
        )
        object.__setattr__(self, "metrics_a", dict(self.metrics_a))
        object.__setattr__(self, "metrics_b", dict(self.metrics_b))
        object.__setattr__(
            self, "measurement_method_a", RuleTestMeasurementMethod(self.measurement_method_a)
        )
        object.__setattr__(
            self, "measurement_method_b", RuleTestMeasurementMethod(self.measurement_method_b)
        )
        for field_name in ("false_positives_added", "false_positives_removed"):
            values = tuple(getattr(self, field_name))
            for value in values:
                _require_sha256(value, field_name)
            object.__setattr__(self, field_name, values)

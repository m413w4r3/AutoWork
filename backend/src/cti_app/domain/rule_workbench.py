from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    DOCUMENTED_SEED_UNCONFIRMED = "documented_seed_unconfirmed"
    NEW_CANDIDATE_UNCONFIRMED = "new_candidate_unconfirmed"
    HUMAN_CONFIRMED = "human_confirmed"
    GOODWARE = "goodware"
    HARD_NEGATIVE = "hard_negative"
    HOLDOUT_POSITIVE = "holdout_positive"
    HOLDOUT_NEGATIVE = "holdout_negative"
    OUT_OF_SCOPE = "out_of_scope"
    BYTES_MISSING = "bytes_missing"

    # Compatibility aliases; the explicit values above are persisted.
    CONFIRMED_POSITIVE = "human_confirmed"
    OUT_OF_CLUSTER_CANDIDATE = "new_candidate_unconfirmed"
    HOLDOUT = "holdout_positive"


class OneRuleTestStatus(StrEnum):
    CANDIDATE_MEASURED = "ONE_RULE_CANDIDATE_MEASURED"
    NOT_VALIDATED = "ONE_RULE_NOT_VALIDATED"


def detect_file_format_magic(content: bytes) -> str:
    """Classify a file from a bounded prefix using its on-disk magic bytes."""
    if content.startswith(b"MZ") and len(content) >= 0x40:
        pe_offset = int.from_bytes(content[0x3C:0x40], "little")
        if pe_offset + 24 <= len(content) and content[pe_offset : pe_offset + 4] == b"PE\0\0":
            optional_header = pe_offset + 24
            magic = int.from_bytes(content[optional_header : optional_header + 2], "little")
            directory_offset = optional_header + (112 if magic == 0x20B else 96)
            clr_entry = directory_offset + 14 * 8
            if len(content) >= clr_entry + 4 and int.from_bytes(
                content[clr_entry : clr_entry + 4], "little"
            ):
                return "pe_cli"
            return "pe"
        return "dos_mz"
    if content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "zip"
    if content.startswith(b"\x7fELF"):
        return "elf"
    if content.startswith(b"\xca\xfe\xba\xbe"):
        return "mach_o_fat"
    if content.startswith((b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe")):
        return "mach_o_32"
    if content.startswith((b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe")):
        return "mach_o_64"
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if content.startswith(b"SQLite format 3\0"):
        return "sqlite"
    if content.startswith(b"\x1f\x8b"):
        return "gzip"
    if content.startswith(b"\0asm"):
        return "wasm"
    prefix = content[:256].lstrip()
    if prefix.startswith(b"<?xml") or prefix.startswith(b"<"):
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError:
            return "unknown"
        return "xml_or_text"
    if prefix and b"\0" not in prefix:
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError:
            return "unknown"
        return "text"
    return "unknown"


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
    format_magic: str = "unknown"
    holdout_designated_at: datetime | None = None
    declared_role: RuleTestCorpusRole | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256, "sample_sha256")
        object.__setattr__(self, "role", RuleTestCorpusRole(self.role))
        if self.declared_role is not None:
            object.__setattr__(self, "declared_role", RuleTestCorpusRole(self.declared_role))
        object.__setattr__(self, "provenance", RuleSampleProvenance(self.provenance))
        if self.expected_match is not None and not isinstance(self.expected_match, bool):
            raise TypeError("expected_match must be a boolean or None")
        if not isinstance(self.format_magic, str) or not self.format_magic.strip():
            raise ValueError("format_magic must describe the detected file format")
        if self.holdout_designated_at is not None:
            if (
                self.holdout_designated_at.tzinfo is None
                or self.holdout_designated_at.utcoffset() is None
            ):
                raise ValueError("holdout designation time must be timezone-aware")
            object.__setattr__(
                self, "holdout_designated_at", self.holdout_designated_at.astimezone(UTC)
            )
        if (
            self.role
            in {
                RuleTestCorpusRole.HOLDOUT_POSITIVE,
                RuleTestCorpusRole.HOLDOUT_NEGATIVE,
            }
            and self.holdout_designated_at is None
        ):
            raise ValueError("holdout cases require a persisted designation time")

    @property
    def expected_result(self) -> bool | None:
        if self.expected_match is not None:
            return self.expected_match
        if self.role in {
            RuleTestCorpusRole.HUMAN_CONFIRMED,
            RuleTestCorpusRole.HOLDOUT_POSITIVE,
        }:
            return True
        if self.role in {
            RuleTestCorpusRole.GOODWARE,
            RuleTestCorpusRole.HARD_NEGATIVE,
            RuleTestCorpusRole.HOLDOUT_NEGATIVE,
        }:
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
                case.format_magic,
                case.holdout_designated_at.isoformat()
                if case.holdout_designated_at is not None
                else "not_holdout",
                case.declared_role.value if case.declared_role is not None else "direct_role",
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
    column: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", RuleTestErrorCode(self.code))
        object.__setattr__(self, "message", self.message[:1000])
        if self.sample_sha256 is not None:
            _require_sha256(self.sample_sha256, "sample_sha256")
        if self.line is not None and (
            isinstance(self.line, bool) or not isinstance(self.line, int) or self.line < 1
        ):
            raise ValueError("line must be positive")
        if self.column is not None and (
            isinstance(self.column, bool) or not isinstance(self.column, int) or self.column < 1
        ):
            raise ValueError("column must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleStringHit:
    identifier: str
    offsets: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.identifier.startswith("$"):
            raise ValueError("YARA string identifiers must begin with '$'")
        offsets = tuple(self.offsets)
        if not offsets or any(isinstance(item, bool) or item < 0 for item in offsets):
            raise ValueError("YARA string hits require non-negative offsets")
        object.__setattr__(self, "offsets", offsets)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleFileCoverage:
    sample_sha256: str
    role: RuleTestCorpusRole
    matched: bool | None
    format_magic: str
    strings: tuple[RuleStringHit, ...] = ()
    details_complete: bool = True
    unavailable_strings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256, "sample_sha256")
        object.__setattr__(self, "role", RuleTestCorpusRole(self.role))
        if self.matched is not None and not isinstance(self.matched, bool):
            raise TypeError("matched must be boolean or None")
        if not self.format_magic:
            raise ValueError("file format magic is required")
        object.__setattr__(self, "strings", tuple(self.strings))
        object.__setattr__(self, "unavailable_strings", tuple(self.unavailable_strings))


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleRoleTestReport:
    role: RuleTestCorpusRole
    corpus_sha256s: tuple[str, ...]
    test_cases: tuple[RuleTestCase, ...] = ()
    matches: tuple[str, ...] = ()
    non_matches: tuple[str, ...] = ()
    non_measurable: tuple[str, ...] = ()
    errors: tuple[RuleTestError, ...] = ()
    file_results: tuple[RuleFileCoverage, ...] = ()

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
        file_results = tuple(self.file_results)
        if any(item.role is not self.role for item in file_results):
            raise ValueError("file coverage role does not match its role report")
        if {item.sample_sha256 for item in file_results} - set(self.corpus_sha256s):
            raise ValueError("file coverage must belong to the declared corpus")
        object.__setattr__(self, "file_results", file_results)
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
        negative_roles = {
            RuleTestCorpusRole.GOODWARE,
            RuleTestCorpusRole.HARD_NEGATIVE,
            RuleTestCorpusRole.HOLDOUT_NEGATIVE,
        }
        return tuple(
            sorted(
                {
                    sample_sha256
                    for item in self.roles
                    if item.role in negative_roles
                    for sample_sha256 in item.matches
                }
            )
        )

    @property
    def false_negative_sha256s(self) -> tuple[str, ...]:
        positive_roles = {
            RuleTestCorpusRole.HUMAN_CONFIRMED,
            RuleTestCorpusRole.HOLDOUT_POSITIVE,
        }
        return tuple(
            sorted(
                {
                    sample_sha256
                    for item in self.roles
                    if item.role in positive_roles
                    for sample_sha256 in item.non_matches
                }
            )
        )

    @property
    def metrics(self) -> dict[str, dict[str, object]]:
        """Rates on real, measured samples; unknown labels never enter denominators."""
        by_role = {item.role: item for item in self.roles}

        def rate(role: RuleTestCorpusRole) -> dict[str, object]:
            result = by_role[role]
            measured_real = {
                case.sample_sha256
                for case in result.test_cases
                if case.provenance is RuleSampleProvenance.REAL
                and case.sample_sha256 in set(result.matches) | set(result.non_matches)
            }
            return wilson_rate(
                successes=len(measured_real & set(result.matches)), trials=len(measured_real)
            )

        return {
            "real_positive_sensitivity": rate(RuleTestCorpusRole.HUMAN_CONFIRMED),
            "real_goodware_false_positive_rate": rate(RuleTestCorpusRole.GOODWARE),
            "real_hard_negative_false_positive_rate": rate(RuleTestCorpusRole.HARD_NEGATIVE),
            "real_holdout_sensitivity": rate(RuleTestCorpusRole.HOLDOUT_POSITIVE),
            "real_holdout_false_positive_rate": rate(RuleTestCorpusRole.HOLDOUT_NEGATIVE),
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
            item for item in self.roles if item.role is RuleTestCorpusRole.HUMAN_CONFIRMED
        )
        goodware = next(item for item in self.roles if item.role is RuleTestCorpusRole.GOODWARE)
        measured_real_positives = [
            case
            for case in positives.test_cases
            if case.provenance is RuleSampleProvenance.REAL
            and case.sample_sha256 in measured_roles[RuleTestCorpusRole.HUMAN_CONFIRMED]
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
    format_magic: str = "unknown"
    designated_at: datetime | None = None
    observation_id: UUID | None = None
    operator_id: str | None = None

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256, "sample_sha256")
        object.__setattr__(self, "role", RuleTestCorpusRole(self.role))
        object.__setattr__(self, "provenance", RuleSampleProvenance(self.provenance))
        if not self.format_magic.strip():
            raise ValueError("format_magic must describe the detected file format")
        if self.designated_at is not None:
            if self.designated_at.tzinfo is None or self.designated_at.utcoffset() is None:
                raise ValueError("designation time must be timezone-aware")
            object.__setattr__(self, "designated_at", self.designated_at.astimezone(UTC))
        if self.role in {
            RuleTestCorpusRole.HOLDOUT_POSITIVE,
            RuleTestCorpusRole.HOLDOUT_NEGATIVE,
        } and (self.designated_at is None or self.observation_id is None):
            raise ValueError("holdout imports require a persisted role and designation time")
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
    column: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", YaraExecutionStatus(self.status))
        object.__setattr__(self, "message", self.message[:1000])
        matches = tuple(self.matches_sha256)
        for sample_sha256 in matches:
            _require_sha256(sample_sha256, "matches_sha256")
        object.__setattr__(self, "matches_sha256", matches)
        if self.line is not None and self.line < 1:
            raise ValueError("line must be positive")
        if self.column is not None and self.column < 1:
            raise ValueError("column must be positive")


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

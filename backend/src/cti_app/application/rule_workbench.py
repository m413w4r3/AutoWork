from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict
from uuid import UUID, uuid4

from cti_app.application.investigation_persistence import DetectionCandidateRepository
from cti_app.domain.analysis import SampleFeatureSetV1
from cti_app.domain.code_features import CodeFeatureSet, CodeFeatureStatus
from cti_app.domain.goodware import (
    Banality,
    GoodwareFeature,
    NonDiscriminantPatternRegistry,
)
from cti_app.domain.invariants import (
    CandidateInvariant,
    CodeFeatureProvenance,
    InvariantCategory,
    InvariantStatus,
    InvariantType,
    ReportClaimProvenance,
    SampleFeatureProvenance,
    m2_feature_kind,
)
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    MotifProvenance,
)
from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.publication import ArtifactType
from cti_app.domain.reference_corpus import ReferenceCorpusVerdict
from cti_app.domain.rule_workbench import (
    RuleBuildResult,
    RuleConditionStrategy,
    RuleMotif,
    RuleMotifIssue,
    RuleMotifIssueCode,
    RuleMotifKind,
    RuleMotifProvenanceKind,
    RuleMotifValidationReport,
    RuleRoleTestReport,
    RuleRoleVersionComparison,
    RuleTestCase,
    RuleTestCorpusRole,
    RuleTestError,
    RuleTestErrorCode,
    RuleTestReport,
    RuleVersionComparison,
    YaraEngineResult,
    YaraExecutionStatus,
)


class RuleCorpusBytesUnavailable(LookupError):
    """The requested quarantined sample is not locally readable."""


class RuleCorpusBytesPort(Protocol):
    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class YaraSampleInput:
    sample_sha256: str
    data: bytes


class YaraEnginePort(Protocol):
    def compile(self, source: str) -> YaraEngineResult: ...

    def match(self, source: str, samples: Sequence[YaraSampleInput]) -> YaraEngineResult: ...


class _MutableRoleResult(TypedDict):
    matches: set[str]
    non_matches: set[str]
    non_measurable: set[str]
    errors: list[RuleTestError]


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleWorkbenchPolicy:
    minimum_string_bytes: int = 6
    minimum_hex_fixed_bytes: int = 4
    minimum_string_entropy_bits: float = 2.2
    minimum_hex_entropy_bits: float = 1.2
    maximum_motifs: int = 128
    maximum_source_bytes: int = 1_000_000
    maximum_metadata_value_chars: int = 2048
    maximum_corpus_cases: int = 256
    maximum_sample_bytes: int = 64 * 1024 * 1024
    maximum_total_sample_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.minimum_string_bytes < 1 or self.minimum_hex_fixed_bytes < 1:
            raise ValueError("minimum motif lengths must be positive")
        if self.minimum_string_entropy_bits < 0 or self.minimum_hex_entropy_bits < 0:
            raise ValueError("entropy limits must be non-negative")
        if self.maximum_motifs < 1 or self.maximum_source_bytes < 1:
            raise ValueError("RuleWorkbench limits must be positive")
        if self.maximum_metadata_value_chars < 1:
            raise ValueError("maximum_metadata_value_chars must be positive")
        if (
            min(
                self.maximum_corpus_cases,
                self.maximum_sample_bytes,
                self.maximum_total_sample_bytes,
            )
            < 1
        ):
            raise ValueError("corpus byte and count limits must be positive")


class RuleRevisionError(ValueError):
    def __init__(
        self, message: str, *, validation: RuleMotifValidationReport | None = None
    ) -> None:
        super().__init__(message)
        self.validation = validation


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleRevisionResult:
    version: DetectionCandidateVersion
    test_report: RuleTestReport


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HEX_TOKEN = re.compile(r"^(?:[0-9A-Fa-f]{2}|[0-9A-Fa-f?][0-9A-Fa-f?])$")
_TRIVIAL_MOTIFS = frozenset(
    {
        "administrator",
        "api",
        "dll",
        "error",
        "http",
        "https",
        "kernel32.dll",
        "microsoft",
        "ntdll.dll",
        "program files",
        "user32.dll",
        "windows",
    }
)
_NOISE_CATEGORIES = frozenset(
    {
        InvariantCategory.LIBRARY_NOISE,
        InvariantCategory.PACKER_ARTIFACT,
        InvariantCategory.COMPILER_ARTIFACT,
        InvariantCategory.GENERIC_WINAPI,
        InvariantCategory.UNKNOWN,
    }
)
_PERSISTED_MOTIF_PREFIX = "rule-workbench-motif-v1:"


class RuleWorkbench:
    def __init__(
        self,
        *,
        corpus_bytes: RuleCorpusBytesPort,
        yara_engine: YaraEnginePort,
        detection_candidates: DetectionCandidateRepository,
        non_discriminant_patterns: NonDiscriminantPatternRegistry,
        legitimate_services: LegitimateServiceRegistry,
        policy: RuleWorkbenchPolicy | None = None,
    ) -> None:
        self._corpus_bytes = corpus_bytes
        self._yara_engine = yara_engine
        self._detection_candidates = detection_candidates
        self._non_discriminant = non_discriminant_patterns
        self._legitimate_services = legitimate_services
        self._policy = policy or RuleWorkbenchPolicy()

    def propose_from_features(
        self,
        invariants: Sequence[CandidateInvariant],
        *,
        feature_sets: Sequence[SampleFeatureSetV1] = (),
        code_feature_sets: Sequence[CodeFeatureSet] = (),
        sample_sha256_by_id: Mapping[UUID, str] | None = None,
        p10_motifs: Sequence[RuleMotif] = (),
        observation_refs_by_invariant: Mapping[UUID, tuple[UUID, ...]] | None = None,
        goodware_features: Sequence[GoodwareFeature] = (),
    ) -> tuple[RuleMotif, ...]:
        """Project measured P09/P10 features into deterministic local motifs."""
        string_encodings: dict[str, RuleMotifKind] = {}
        for sample_feature_set in feature_sets:
            for feature in sample_feature_set.strings:
                if not isinstance(feature, Mapping):
                    continue
                value = feature.get("value", feature.get("name"))
                encoding = feature.get("encoding", feature.get("type", ""))
                if isinstance(value, str) and isinstance(encoding, str):
                    if any(token in encoding.casefold() for token in ("wide", "utf-16", "unicode")):
                        string_encodings[value] = RuleMotifKind.WIDE

        code_index: dict[tuple[str, int, int, str], bool] = {}
        sample_hashes = sample_sha256_by_id or {}
        for code_feature_set in code_feature_sets:
            if code_feature_set.status is not CodeFeatureStatus.SUCCEEDED:
                continue
            feature_sample_sha256 = sample_hashes.get(code_feature_set.sample_id)
            if feature_sample_sha256 is None:
                continue
            for ngram in code_feature_set.ngrams:
                code_index[
                    (
                        feature_sample_sha256,
                        ngram.function_offset,
                        ngram.start_offset,
                        ngram.pattern,
                    )
                ] = True

        goodware_counts = {
            (feature.feature_kind, feature.normalized_value.casefold()): feature.occurrence_count
            for feature in goodware_features
        }
        observations = observation_refs_by_invariant or {}
        candidates: list[RuleMotif] = list(
            motif
            for motif in p10_motifs
            if motif.provenance_kind is RuleMotifProvenanceKind.P10_PROPOSAL
        )

        for invariant in sorted(invariants, key=lambda item: str(item.id)):
            if invariant.status in {
                InvariantStatus.REJECTED,
                InvariantStatus.UNSELECTIVE,
                InvariantStatus.SHARED_COMPONENT,
            }:
                continue
            if invariant.category in _NOISE_CATEGORIES or invariant.banality is Banality.BANAL:
                continue
            if invariant.corpus_verdict is ReferenceCorpusVerdict.MULTI_FAMILY:
                continue
            if invariant.positive_support == 0:
                continue
            if (
                any(
                    isinstance(provenance, ReportClaimProvenance)
                    for provenance in invariant.provenances
                )
                and not invariant.positive_sample_confirmed
            ):
                continue

            motif_kind = self._motif_kind(invariant, string_encodings)
            if motif_kind is None:
                continue
            value = invariant.masked_pattern if invariant.type is InvariantType.CODE_NGRAM else None
            pattern = value or invariant.pattern
            if self._is_known_non_discriminant(invariant, pattern):
                continue

            provenance_items = sorted(
                invariant.provenances,
                key=lambda item: json.dumps(
                    item.as_canonical_dict(), sort_keys=True, separators=(",", ":")
                ),
            )
            sample_sha256: str | None = None
            offsets: tuple[int, ...] | None = None
            for provenance in provenance_items:
                if isinstance(provenance, SampleFeatureProvenance):
                    sample_sha256, offsets = provenance.sample_sha256, provenance.offsets
                    break
                if isinstance(provenance, CodeFeatureProvenance):
                    sample_sha256 = provenance.sample_sha256
                    offsets = (provenance.offset,)
                    expected_address = _address_value(provenance.function_address)
                    if code_feature_sets and not code_index.get(
                        (sample_sha256, expected_address, provenance.offset, pattern), False
                    ):
                        sample_sha256, offsets = None, None
                        continue
                    break
            if offsets is not None and not sample_sha256:
                offsets = None

            try:
                descriptor = m2_feature_kind(invariant.type, invariant.pattern)
            except (TypeError, ValueError):
                descriptor = None
            goodware_count = (
                goodware_counts.get((descriptor[0], descriptor[1].casefold()))
                if descriptor is not None
                else None
            )
            candidates.append(
                RuleMotif(
                    kind=motif_kind,
                    value=pattern,
                    provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
                    provenance_id=str(invariant.id),
                    observation_refs=observations.get(invariant.id, ()),
                    sample_sha256=sample_sha256,
                    offsets=offsets,
                    rationale=(
                        f"type={invariant.type.value}; category={invariant.category.value}; "
                        f"banality={invariant.banality.value}; "
                        f"corpus={invariant.corpus_verdict.value}"
                    ),
                    positive_support=invariant.positive_support,
                    benign_prevalence=invariant.benign_prevalence,
                    goodware_occurrence_count=goodware_count,
                )
            )

        unique: dict[tuple[str, str], RuleMotif] = {}
        for motif in sorted(
            candidates,
            key=lambda item: (
                item.kind.value,
                item.value.casefold(),
                item.provenance_kind.value,
                item.provenance_id,
            ),
        ):
            unique.setdefault(_motif_identity(motif), motif)
        return tuple(unique[key] for key in sorted(unique))

    def validate_motifs(
        self,
        motifs: Sequence[RuleMotif],
        *,
        meta: Mapping[str, str | int | bool] | None = None,
    ) -> RuleMotifValidationReport:
        issues: list[RuleMotifIssue] = []
        if not isinstance(motifs, Sequence):
            return RuleMotifValidationReport(
                issues=(
                    RuleMotifIssue(
                        code=RuleMotifIssueCode.EMPTY,
                        message="motifs must be a sequence",
                    ),
                )
            )
        if not motifs:
            issues.append(
                RuleMotifIssue(
                    code=RuleMotifIssueCode.EMPTY, message="at least one motif is required"
                )
            )
        if len(motifs) > self._policy.maximum_motifs:
            issues.append(
                RuleMotifIssue(
                    code=RuleMotifIssueCode.TOO_MANY_MOTIFS,
                    message=f"rule exceeds {self._policy.maximum_motifs} motifs",
                )
            )
        seen: set[tuple[str, str]] = set()
        for index, motif in enumerate(motifs):
            if not isinstance(motif, RuleMotif):
                issues.append(
                    RuleMotifIssue(
                        code=RuleMotifIssueCode.INVALID_METADATA,
                        motif_index=index,
                        message="each item must be a typed RuleMotif",
                    )
                )
                continue
            identity = _motif_identity(motif)
            if identity in seen:
                issues.append(
                    RuleMotifIssue(
                        code=RuleMotifIssueCode.TRIVIAL,
                        motif_index=index,
                        message="duplicate motifs do not add discrimination",
                    )
                )
            seen.add(identity)
            if self._is_trivial(motif.value):
                issues.append(
                    RuleMotifIssue(
                        code=RuleMotifIssueCode.TRIVIAL,
                        motif_index=index,
                        message="motif is a common or low-information value",
                    )
                )
                continue
            if self._is_known(motif):
                issues.append(
                    RuleMotifIssue(
                        code=RuleMotifIssueCode.KNOWN_NON_DISCRIMINANT,
                        motif_index=index,
                        message="motif is listed as a known non-discriminant value",
                    )
                )
                continue

            if motif.kind in {RuleMotifKind.HEX, RuleMotifKind.CODE_SEQUENCE}:
                tokens = motif.value.split()
                if not tokens or any(_HEX_TOKEN.fullmatch(token) is None for token in tokens):
                    issues.append(
                        RuleMotifIssue(
                            code=RuleMotifIssueCode.INVALID_HEX,
                            motif_index=index,
                            message="hex motifs must contain byte or nibble-wildcard tokens",
                        )
                    )
                    continue
                fixed_bytes = [
                    int(token, 16) for token in tokens if re.fullmatch(r"[0-9A-Fa-f]{2}", token)
                ]
                if len(fixed_bytes) < self._policy.minimum_hex_fixed_bytes:
                    issues.append(
                        RuleMotifIssue(
                            code=RuleMotifIssueCode.TOO_SHORT,
                            motif_index=index,
                            message="hex motif has too few fixed bytes",
                        )
                    )
                elif _entropy(fixed_bytes) < self._policy.minimum_hex_entropy_bits:
                    issues.append(
                        RuleMotifIssue(
                            code=RuleMotifIssueCode.LOW_ENTROPY,
                            motif_index=index,
                            message="hex motif has low byte entropy",
                        )
                    )
            else:
                encoded = motif.value.encode("utf-8")
                if len(encoded) < self._policy.minimum_string_bytes:
                    issues.append(
                        RuleMotifIssue(
                            code=RuleMotifIssueCode.TOO_SHORT,
                            motif_index=index,
                            message="string motif is shorter than the configured minimum",
                        )
                    )
                elif _entropy(encoded) < self._policy.minimum_string_entropy_bits:
                    issues.append(
                        RuleMotifIssue(
                            code=RuleMotifIssueCode.LOW_ENTROPY,
                            motif_index=index,
                            message="string motif has low byte entropy",
                        )
                    )

        if meta is not None and not isinstance(meta, Mapping):
            issues.append(
                RuleMotifIssue(
                    code=RuleMotifIssueCode.INVALID_METADATA,
                    message="metadata must be a mapping",
                )
            )
        metadata_items = tuple(meta.items()) if isinstance(meta, Mapping) else ()
        for key, value in sorted(metadata_items, key=lambda item: repr(item[0])):
            if (
                not isinstance(key, str)
                or _IDENTIFIER.fullmatch(key) is None
                or not isinstance(value, (str, int, bool))
                or len(str(value)) > self._policy.maximum_metadata_value_chars
            ):
                issues.append(
                    RuleMotifIssue(
                        code=RuleMotifIssueCode.INVALID_METADATA,
                        message="metadata keys must be safe identifiers and values must be bounded",
                    )
                )
        return RuleMotifValidationReport(issues=tuple(issues))

    def build_rule(
        self,
        motifs: Sequence[RuleMotif],
        condition_strategy: RuleConditionStrategy | str,
        meta: Mapping[str, str | int | bool] | None = None,
    ) -> RuleBuildResult:
        try:
            strategy = RuleConditionStrategy(condition_strategy)
        except (TypeError, ValueError):
            report = RuleMotifValidationReport(
                issues=(
                    RuleMotifIssue(
                        code=RuleMotifIssueCode.INVALID_METADATA,
                        message="condition strategy is not supported",
                    ),
                )
            )
            return RuleBuildResult(source=None, rule_identifier=None, validation=report)

        if meta is not None and not isinstance(meta, Mapping):
            report = self.validate_motifs(motifs, meta=meta)
            return RuleBuildResult(source=None, rule_identifier=None, validation=report)
        supplied_meta = dict(meta or {})
        supplied_meta["workbench_condition"] = strategy.value
        report = self.validate_motifs(motifs, meta=supplied_meta)
        if not report.valid:
            return RuleBuildResult(source=None, rule_identifier=None, validation=report)

        canonical_motifs = tuple(
            sorted(
                motifs, key=lambda motif: (motif.kind.value, motif.value.casefold(), motif.value)
            )
        )
        identity = hashlib.sha256(
            json.dumps(
                {
                    "condition": strategy.value,
                    "meta": supplied_meta,
                    "motifs": [
                        {
                            "kind": motif.kind.value,
                            "value": motif.value,
                            "provenance": f"{motif.provenance_kind.value}:{motif.provenance_id}",
                        }
                        for motif in canonical_motifs
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        rule_identifier = f"autowork_{identity[:20]}"
        rendered_meta: dict[str, str | int | bool] = {
            "generated_by": "AutoWork RuleWorkbench",
            **supplied_meta,
        }
        for index, motif in enumerate(canonical_motifs):
            rendered_meta[f"motif_{index}_kind"] = motif.kind.value
            rendered_meta[f"motif_{index}_provenance"] = (
                f"{motif.provenance_kind.value}:{motif.provenance_id}"
            )
            if motif.observation_refs:
                rendered_meta[f"motif_{index}_observations"] = ",".join(
                    str(reference) for reference in motif.observation_refs
                )
            if motif.sample_sha256 is not None:
                rendered_meta[f"motif_{index}_sample_sha256"] = motif.sample_sha256
            if motif.offsets is not None:
                rendered_meta[f"motif_{index}_offsets"] = ",".join(
                    str(value) for value in motif.offsets
                )
            if motif.positive_support is not None:
                rendered_meta[f"motif_{index}_positive_support"] = motif.positive_support
            if motif.benign_prevalence is not None:
                rendered_meta[f"motif_{index}_benign_prevalence"] = motif.benign_prevalence
            if motif.goodware_occurrence_count is not None:
                rendered_meta[f"motif_{index}_goodware_occurrences"] = (
                    motif.goodware_occurrence_count
                )

        lines = [f"rule {rule_identifier} {{", "    meta:"]
        for key, value in sorted(rendered_meta.items()):
            lines.append(f"        {key} = {_render_meta_value(value)}")
        lines.extend(("    strings:",))
        for index, motif in enumerate(canonical_motifs):
            lines.append(f"        $m{index} = {_render_motif(motif)}")
        lines.extend(
            ("    condition:", f"        {_condition(strategy, len(canonical_motifs))}", "}")
        )
        source = "\n".join(lines) + "\n"
        if len(source.encode("utf-8")) > self._policy.maximum_source_bytes:
            report = RuleMotifValidationReport(
                issues=(
                    RuleMotifIssue(
                        code=RuleMotifIssueCode.INVALID_METADATA,
                        message="rendered rule exceeds the configured source size limit",
                    ),
                )
            )
            return RuleBuildResult(source=None, rule_identifier=None, validation=report)
        return RuleBuildResult(source=source, rule_identifier=rule_identifier, validation=report)

    async def compile_and_test(
        self,
        rule_source: str,
        corpus: Sequence[RuleTestCase],
        *,
        selection_bias: str,
        coverage_statement: str,
    ) -> RuleTestReport:
        source_sha256 = hashlib.sha256(rule_source.encode("utf-8")).hexdigest()
        cases = _canonical_corpus(corpus)
        by_role: dict[RuleTestCorpusRole, list[RuleTestCase]] = {
            role: [case for case in cases if case.role is role] for role in RuleTestCorpusRole
        }
        results: dict[RuleTestCorpusRole, _MutableRoleResult] = {
            role: {"matches": set(), "non_matches": set(), "non_measurable": set(), "errors": []}
            for role in RuleTestCorpusRole
        }
        try:
            compilation = self._yara_engine.compile(rule_source)
        except Exception:
            compilation = YaraEngineResult(
                status=YaraExecutionStatus.ENGINE_CRASHED,
                engine_version="unknown",
                message="YARA compiler failed unexpectedly",
            )
        status = compilation.status
        if status is not YaraExecutionStatus.OK:
            for role, role_cases in by_role.items():
                errors = results[role]["errors"]
                for case in role_cases:
                    errors.append(
                        RuleTestError(
                            code=_test_error_code(status),
                            message=compilation.message or status.value,
                            sample_sha256=case.sample_sha256,
                            line=compilation.line,
                        )
                    )
            return self._report(
                source_sha256,
                status,
                compilation.engine_version,
                by_role,
                results,
                selection_bias,
                coverage_statement,
            )

        if len(cases) > self._policy.maximum_corpus_cases:
            return self._limit_failure_report(
                source_sha256,
                compilation.engine_version,
                by_role,
                results,
                selection_bias,
                coverage_statement,
                "corpus exceeds the configured case count",
            )

        sample_bytes: dict[str, bytes] = {}
        unavailable: set[str] = set()
        corpus_errors: dict[str, RuleTestError] = {}
        total_sample_bytes = 0
        for case in cases:
            if (
                case.sample_sha256 in sample_bytes
                or case.sample_sha256 in unavailable
                or case.sample_sha256 in corpus_errors
            ):
                continue
            try:
                payload = await self._corpus_bytes.get_bytes_for_sample(case.sample_sha256)
            except RuleCorpusBytesUnavailable:
                unavailable.add(case.sample_sha256)
                continue
            except Exception:
                corpus_errors[case.sample_sha256] = RuleTestError(
                    code=RuleTestErrorCode.CORPUS_ERROR,
                    message="quarantined sample bytes could not be resolved",
                    sample_sha256=case.sample_sha256,
                )
                continue
            if (
                not isinstance(payload, bytes)
                or hashlib.sha256(payload).hexdigest() != case.sample_sha256
            ):
                corpus_errors[case.sample_sha256] = RuleTestError(
                    code=RuleTestErrorCode.INVALID_SAMPLE,
                    message="resolved bytes do not match the declared sample SHA-256",
                    sample_sha256=case.sample_sha256,
                )
                continue
            if (
                len(payload) > self._policy.maximum_sample_bytes
                or total_sample_bytes + len(payload) > self._policy.maximum_total_sample_bytes
            ):
                return self._limit_failure_report(
                    source_sha256,
                    compilation.engine_version,
                    by_role,
                    results,
                    selection_bias,
                    coverage_statement,
                    "corpus exceeds the configured sample byte limit",
                )
            sample_bytes[case.sample_sha256] = payload
            total_sample_bytes += len(payload)

        for case in cases:
            role_result = results[case.role]
            if case.sample_sha256 in unavailable:
                non_measurable = role_result["non_measurable"]
                non_measurable.add(case.sample_sha256)
            elif case.sample_sha256 in corpus_errors:
                errors = role_result["errors"]
                errors.append(corpus_errors[case.sample_sha256])

        if sample_bytes:
            samples = tuple(
                YaraSampleInput(sample_sha256=sample_sha256, data=sample_bytes[sample_sha256])
                for sample_sha256 in sorted(sample_bytes)
            )
            try:
                matching = self._yara_engine.match(rule_source, samples)
            except Exception:
                matching = YaraEngineResult(
                    status=YaraExecutionStatus.ENGINE_CRASHED,
                    engine_version=compilation.engine_version,
                    message="YARA matcher failed unexpectedly",
                )
            status = matching.status
            matched = set(matching.matches_sha256)
            for case in cases:
                if case.sample_sha256 not in sample_bytes:
                    continue
                role_result = results[case.role]
                if matching.status is YaraExecutionStatus.OK:
                    target = (
                        role_result["matches"]
                        if case.sample_sha256 in matched
                        else role_result["non_matches"]
                    )
                    target.add(case.sample_sha256)
                else:
                    errors = role_result["errors"]
                    errors.append(
                        RuleTestError(
                            code=_test_error_code(matching.status),
                            message=matching.message or matching.status.value,
                            sample_sha256=case.sample_sha256,
                            line=matching.line,
                        )
                    )
        return self._report(
            source_sha256,
            status,
            compilation.engine_version,
            by_role,
            results,
            selection_bias,
            coverage_statement,
        )

    async def revise(
        self,
        parent_version: DetectionCandidateVersion | None,
        *,
        new_motifs: Sequence[RuleMotif] | None = None,
        new_condition: RuleConditionStrategy | str | None = None,
        corpus: Sequence[RuleTestCase],
        selection_bias: str,
        coverage_statement: str,
        investigation_id: UUID | None = None,
        candidate_id: UUID | None = None,
        meta: Mapping[str, str | int | bool] | None = None,
    ) -> DetectionCandidateVersion:
        result = await self.revise_with_report(
            parent_version,
            new_motifs=new_motifs,
            new_condition=new_condition,
            corpus=corpus,
            selection_bias=selection_bias,
            coverage_statement=coverage_statement,
            investigation_id=investigation_id,
            candidate_id=candidate_id,
            meta=meta,
        )
        return result.version

    async def revise_with_report(
        self,
        parent_version: DetectionCandidateVersion | None,
        *,
        new_motifs: Sequence[RuleMotif] | None = None,
        new_condition: RuleConditionStrategy | str | None = None,
        corpus: Sequence[RuleTestCase],
        selection_bias: str,
        coverage_statement: str,
        investigation_id: UUID | None = None,
        candidate_id: UUID | None = None,
        meta: Mapping[str, str | int | bool] | None = None,
    ) -> RuleRevisionResult:
        if parent_version is None:
            if investigation_id is None:
                raise RuleRevisionError("investigation_id is required for v1")
            version_number = 1
            parent_id = None
            resolved_candidate_id = candidate_id or uuid4()
            current_motifs = tuple(new_motifs or ())
            if new_condition is None:
                raise RuleRevisionError("condition strategy is required for v1")
            strategy = _coerce_condition(new_condition)
        else:
            if investigation_id is not None or candidate_id is not None:
                raise RuleRevisionError(
                    "investigation_id and candidate_id cannot override a parent"
                )
            if parent_version.status is DetectionCandidateStatus.APPROVED:
                raise RuleRevisionError("an approved version is immutable and cannot be revised")
            version_number = parent_version.version + 1
            parent_id = parent_version.id
            resolved_candidate_id = parent_version.candidate_id
            current_motifs = (
                tuple(new_motifs)
                if new_motifs is not None
                else _motifs_from_version(parent_version)
            )
            strategy = (
                _coerce_condition(new_condition)
                if new_condition is not None
                else _condition_from_source(parent_version.source)
            )
            if new_motifs is None and new_condition is None:
                raise RuleRevisionError("a revision must change motifs or condition")

        validation = self.validate_motifs(current_motifs, meta=meta)
        if not validation.valid:
            raise RuleRevisionError("motifs or metadata failed validation", validation=validation)
        if any(not motif.observation_refs for motif in current_motifs):
            issue = RuleMotifIssue(
                code=RuleMotifIssueCode.MISSING_OBSERVATION,
                message="persisted motifs must reference at least one evidence observation",
            )
            raise RuleRevisionError(
                "motifs require persisted evidence observation references",
                validation=RuleMotifValidationReport(issues=(issue,)),
            )

        build = self.build_rule(current_motifs, strategy, meta)
        if not build.valid or build.source is None:
            raise RuleRevisionError("rule could not be rendered", validation=build.validation)
        report = await self.compile_and_test(
            build.source,
            corpus,
            selection_bias=selection_bias,
            coverage_statement=coverage_statement,
        )
        status = _candidate_status(report)
        motif_provenance = tuple(
            MotifProvenance(
                motif=motif.value,
                rationale=_serialize_motif(motif),
                observation_refs=motif.observation_refs,
            )
            for motif in sorted(
                current_motifs,
                key=lambda item: (item.kind.value, item.value.casefold(), item.provenance_id),
            )
        )
        version = DetectionCandidateVersion(
            investigation_id=(
                parent_version.investigation_id
                if parent_version
                else _required_investigation_id(investigation_id)
            ),
            candidate_id=resolved_candidate_id,
            source=build.source,
            source_sha256=hashlib.sha256(build.source.encode("utf-8")).hexdigest(),
            motif_provenance=motif_provenance,
            status=status,
            version=version_number,
            parent_version_id=parent_id,
            test_refs=tuple(dict.fromkeys(case.id for case in _canonical_corpus(corpus))),
        )
        stored_version = await self._detection_candidates.append_version(
            version, expected_previous_id=parent_id
        )
        return RuleRevisionResult(version=stored_version, test_report=report)

    async def compare(
        self,
        version_a: DetectionCandidateVersion,
        version_b: DetectionCandidateVersion,
        corpus: Sequence[RuleTestCase],
        *,
        selection_bias: str,
        coverage_statement: str,
    ) -> RuleVersionComparison:
        if (
            version_a.candidate_id != version_b.candidate_id
            or version_a.investigation_id != version_b.investigation_id
        ):
            raise ValueError("versions must belong to the same detection candidate")
        report_a, report_b = await self._test_versions(
            version_a,
            version_b,
            corpus,
            selection_bias=selection_bias,
            coverage_statement=coverage_statement,
        )
        roles: list[RuleRoleVersionComparison] = []
        for role in RuleTestCorpusRole:
            result_a = _role_report(report_a, role)
            result_b = _role_report(report_b, role)
            measurable_a = set(result_a.matches) | set(result_a.non_matches)
            measurable_b = set(result_b.matches) | set(result_b.non_matches)
            comparable = measurable_a & measurable_b
            matches_a = set(result_a.matches) & comparable
            matches_b = set(result_b.matches) & comparable
            corpus_hashes = set(result_a.corpus_sha256s) | set(result_b.corpus_sha256s)
            roles.append(
                RuleRoleVersionComparison(
                    role=role,
                    matches_gained=tuple(sorted(matches_b - matches_a)),
                    matches_lost=tuple(sorted(matches_a - matches_b)),
                    non_comparable=tuple(sorted(corpus_hashes - comparable)),
                )
            )
        goodware_result_a = _role_report(report_a, RuleTestCorpusRole.GOODWARE)
        goodware_result_b = _role_report(report_b, RuleTestCorpusRole.GOODWARE)
        comparable_goodware = (
            set(goodware_result_a.matches) | set(goodware_result_a.non_matches)
        ) & (set(goodware_result_b.matches) | set(goodware_result_b.non_matches))
        goodware_a = set(goodware_result_a.matches) & comparable_goodware
        goodware_b = set(goodware_result_b.matches) & comparable_goodware
        return RuleVersionComparison(
            version_a_id=version_a.id,
            version_b_id=version_b.id,
            version_a=version_a.version_label,
            version_b=version_b.version_label,
            roles=tuple(roles),
            false_positives_added=tuple(sorted(goodware_b - goodware_a)),
            false_positives_removed=tuple(sorted(goodware_a - goodware_b)),
            engine_version_a=report_a.engine_version,
            engine_version_b=report_b.engine_version,
        )

    async def _test_versions(
        self,
        version_a: DetectionCandidateVersion,
        version_b: DetectionCandidateVersion,
        corpus: Sequence[RuleTestCase],
        *,
        selection_bias: str,
        coverage_statement: str,
    ) -> tuple[RuleTestReport, RuleTestReport]:
        # Keep engine calls sequential so the CPU/memory limits apply predictably.
        report_a = await self.compile_and_test(
            version_a.source,
            corpus,
            selection_bias=selection_bias,
            coverage_statement=coverage_statement,
        )
        report_b = await self.compile_and_test(
            version_b.source,
            corpus,
            selection_bias=selection_bias,
            coverage_statement=coverage_statement,
        )
        return report_a, report_b

    def _report(
        self,
        source_sha256: str,
        status: YaraExecutionStatus,
        engine_version: str,
        by_role: Mapping[RuleTestCorpusRole, Sequence[RuleTestCase]],
        results: Mapping[RuleTestCorpusRole, _MutableRoleResult],
        selection_bias: str,
        coverage_statement: str,
    ) -> RuleTestReport:
        role_reports: list[RuleRoleTestReport] = []
        for role in RuleTestCorpusRole:
            result = results[role]
            errors = result["errors"]
            role_reports.append(
                RuleRoleTestReport(
                    role=role,
                    corpus_sha256s=tuple(sorted({case.sample_sha256 for case in by_role[role]})),
                    test_cases=tuple(by_role[role]),
                    matches=tuple(sorted(result["matches"])),
                    non_matches=tuple(sorted(result["non_matches"])),
                    non_measurable=tuple(sorted(result["non_measurable"])),
                    errors=tuple(errors),
                )
            )
        return RuleTestReport(
            source_sha256=source_sha256,
            execution_status=status,
            engine_version=engine_version or "unknown",
            roles=tuple(role_reports),
            selection_bias=selection_bias,
            coverage_statement=coverage_statement,
        )

    def _limit_failure_report(
        self,
        source_sha256: str,
        engine_version: str,
        by_role: Mapping[RuleTestCorpusRole, Sequence[RuleTestCase]],
        results: Mapping[RuleTestCorpusRole, _MutableRoleResult],
        selection_bias: str,
        coverage_statement: str,
        message: str,
    ) -> RuleTestReport:
        for role, cases in by_role.items():
            result = results[role]
            result["matches"].clear()
            result["non_matches"].clear()
            result["non_measurable"].clear()
            result["errors"].clear()
            result["errors"].extend(
                RuleTestError(
                    code=RuleTestErrorCode.RESOURCE_EXCEEDED,
                    message=message,
                    sample_sha256=case.sample_sha256,
                )
                for case in cases
            )
        return self._report(
            source_sha256,
            YaraExecutionStatus.RESOURCE_EXCEEDED,
            engine_version,
            by_role,
            results,
            selection_bias,
            coverage_statement,
        )

    def _motif_kind(
        self,
        invariant: CandidateInvariant,
        string_encodings: Mapping[str, RuleMotifKind],
    ) -> RuleMotifKind | None:
        if invariant.type is InvariantType.LITERAL_STRING:
            return string_encodings.get(invariant.pattern, RuleMotifKind.STRING)
        if invariant.type in {
            InvariantType.HEX_PATTERN,
            InvariantType.OPCODE_SEQUENCE,
            InvariantType.CODE_NGRAM,
        }:
            return (
                RuleMotifKind.CODE_SEQUENCE
                if invariant.type
                in {
                    InvariantType.OPCODE_SEQUENCE,
                    InvariantType.CODE_NGRAM,
                }
                else RuleMotifKind.HEX
            )
        if invariant.type in {
            InvariantType.IMPORT_NAME,
            InvariantType.EXPORT_NAME,
            InvariantType.SECTION_NAME,
            InvariantType.CAPABILITY,
        }:
            return RuleMotifKind.STRING
        return None

    def _is_known_non_discriminant(self, invariant: CandidateInvariant, pattern: str) -> bool:
        try:
            descriptor = m2_feature_kind(invariant.type, invariant.pattern)
        except (TypeError, ValueError):
            descriptor = None
        if descriptor is not None:
            if self._non_discriminant.lookup(descriptor[0], descriptor[1].casefold()) is not None:
                return True
        return self._is_legitimate_service(pattern)

    def _is_known(self, motif: RuleMotif) -> bool:
        if self._is_legitimate_service(motif.value):
            return True
        feature_kinds = {
            RuleMotifKind.STRING: ("string", "import", "export", "section", "capability"),
            RuleMotifKind.WIDE: ("string",),
            RuleMotifKind.BASE64: ("string",),
            RuleMotifKind.HEX: ("opcode_fragment16", "hex_pattern"),
            RuleMotifKind.CODE_SEQUENCE: ("code_ngram", "opcode_fragment16"),
        }[motif.kind]
        normalized = motif.value.casefold()
        return any(
            self._non_discriminant.lookup(feature_kind, normalized) is not None
            for feature_kind in feature_kinds
        )

    def _is_legitimate_service(self, value: str) -> bool:
        return (
            self._legitimate_services.match_indicator(ArtifactType.DOMAIN, value) is not None
            or self._legitimate_services.match_indicator(ArtifactType.URL, value) is not None
        )

    def _is_trivial(self, value: str) -> bool:
        return self._is_trivial_value(value) or self._is_legitimate_service(value)

    @staticmethod
    def _is_trivial_value(value: str) -> bool:
        normalized = " ".join(value.casefold().split())
        if normalized in _TRIVIAL_MOTIFS:
            return True
        return len(normalized) >= 4 and normalized.isdigit()


def _canonical_corpus(corpus: Sequence[RuleTestCase]) -> tuple[RuleTestCase, ...]:
    unique: dict[tuple[RuleTestCorpusRole, str], RuleTestCase] = {}
    for case in corpus:
        unique.setdefault((case.role, case.sample_sha256), case)
    return tuple(unique[key] for key in sorted(unique, key=lambda item: (item[0].value, item[1])))


def _entropy(data: Sequence[int] | bytes) -> float:
    if not data:
        return 0.0
    size = len(data)
    return -sum((count / size) * math.log2(count / size) for count in Counter(data).values())


def _motif_identity(motif: RuleMotif) -> tuple[str, str]:
    value = motif.value
    if motif.kind in {RuleMotifKind.HEX, RuleMotifKind.CODE_SEQUENCE}:
        value = " ".join(token.upper() for token in value.split())
    return motif.kind.value, value


def _render_motif(motif: RuleMotif) -> str:
    if motif.kind in {RuleMotifKind.HEX, RuleMotifKind.CODE_SEQUENCE}:
        return "{ " + " ".join(token.upper() for token in motif.value.split()) + " }"
    modifier = {
        RuleMotifKind.STRING: "ascii",
        RuleMotifKind.WIDE: "wide",
        RuleMotifKind.BASE64: "base64",
    }[motif.kind]
    return f'"{_escape_yara_string(motif.value)}" {modifier}'


def _escape_yara_string(value: str) -> str:
    escaped: list[str] = []
    for byte in value.encode("utf-8"):
        if byte == 0x22:
            escaped.append('\\"')
        elif byte == 0x5C:
            escaped.append("\\\\")
        elif 0x20 <= byte <= 0x7E:
            escaped.append(chr(byte))
        else:
            escaped.append(f"\\x{byte:02X}")
    return "".join(escaped)


def _render_meta_value(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return f'"{_escape_yara_string(value)}"'


def _condition(strategy: RuleConditionStrategy, count: int) -> str:
    if strategy is RuleConditionStrategy.ANY:
        return "any of them"
    if strategy is RuleConditionStrategy.ALL:
        return "all of them"
    return f"{max(1, math.ceil(count / 2))} of them"


def _address_value(value: int | str) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(value, 0)
    except ValueError:
        return -1


def _serialize_motif(motif: RuleMotif) -> str:
    payload = {
        "kind": motif.kind.value,
        "provenance_kind": motif.provenance_kind.value,
        "provenance_id": motif.provenance_id,
        "sample_sha256": motif.sample_sha256,
        "offsets": list(motif.offsets) if motif.offsets is not None else None,
        "rationale": motif.rationale,
        "positive_support": motif.positive_support,
        "benign_prevalence": motif.benign_prevalence,
        "goodware_occurrence_count": motif.goodware_occurrence_count,
    }
    return _PERSISTED_MOTIF_PREFIX + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _motifs_from_version(version: DetectionCandidateVersion) -> tuple[RuleMotif, ...]:
    motifs: list[RuleMotif] = []
    for saved in version.motif_provenance:
        if not saved.rationale.startswith(_PERSISTED_MOTIF_PREFIX):
            raise RuleRevisionError("parent motif provenance is not a RuleWorkbench version")
        try:
            payload = json.loads(saved.rationale[len(_PERSISTED_MOTIF_PREFIX) :])
            motif = RuleMotif(
                kind=RuleMotifKind(payload["kind"]),
                value=saved.motif,
                provenance_kind=RuleMotifProvenanceKind(payload["provenance_kind"]),
                provenance_id=payload["provenance_id"],
                observation_refs=saved.observation_refs,
                sample_sha256=payload.get("sample_sha256"),
                offsets=tuple(payload["offsets"]) if payload.get("offsets") is not None else None,
                rationale=payload.get("rationale", ""),
                positive_support=payload.get("positive_support"),
                benign_prevalence=payload.get("benign_prevalence"),
                goodware_occurrence_count=payload.get("goodware_occurrence_count"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuleRevisionError("parent motif provenance is invalid") from exc
        motifs.append(motif)
    return tuple(motifs)


def _condition_from_source(source: str) -> RuleConditionStrategy:
    match = re.search(
        r'^\s*workbench_condition\s*=\s*"(any|all|majority)"\s*$', source, re.MULTILINE
    )
    if match is None:
        raise RuleRevisionError("parent source has no RuleWorkbench condition metadata")
    return RuleConditionStrategy(match.group(1))


def _coerce_condition(value: RuleConditionStrategy | str) -> RuleConditionStrategy:
    try:
        return RuleConditionStrategy(value)
    except (TypeError, ValueError) as exc:
        raise RuleRevisionError("condition strategy is not supported") from exc


def _test_error_code(status: YaraExecutionStatus) -> RuleTestErrorCode:
    return {
        YaraExecutionStatus.COMPILE_ERROR: RuleTestErrorCode.COMPILE_ERROR,
        YaraExecutionStatus.TIMEOUT: RuleTestErrorCode.TIMEOUT,
        YaraExecutionStatus.RESOURCE_EXCEEDED: RuleTestErrorCode.RESOURCE_EXCEEDED,
        YaraExecutionStatus.ENGINE_UNAVAILABLE: RuleTestErrorCode.ENGINE_UNAVAILABLE,
        YaraExecutionStatus.ENGINE_CRASHED: RuleTestErrorCode.ENGINE_CRASHED,
        YaraExecutionStatus.INVALID_INPUT: RuleTestErrorCode.INVALID_SAMPLE,
        YaraExecutionStatus.OK: RuleTestErrorCode.ENGINE_CRASHED,
    }[status]


def _candidate_status(report: RuleTestReport) -> DetectionCandidateStatus:
    if report.execution_status is YaraExecutionStatus.COMPILE_ERROR:
        return DetectionCandidateStatus.REJECTED
    if any(role.matches for role in report.roles if role.role is RuleTestCorpusRole.GOODWARE):
        return DetectionCandidateStatus.REJECTED
    positives = next(
        role for role in report.roles if role.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
    )
    if (
        positives.matches
        and not positives.non_matches
        and not positives.non_measurable
        and not positives.errors
    ):
        if report.execution_status is YaraExecutionStatus.OK:
            return DetectionCandidateStatus.TESTED
    return DetectionCandidateStatus.CANDIDATE


def _role_report(report: RuleTestReport, role: RuleTestCorpusRole) -> RuleRoleTestReport:
    return next(result for result in report.roles if result.role is role)


def _required_investigation_id(investigation_id: UUID | None) -> UUID:
    if investigation_id is None:
        raise RuleRevisionError("investigation_id is required for v1")
    return investigation_id


__all__ = [
    "RuleCorpusBytesPort",
    "RuleCorpusBytesUnavailable",
    "RuleRevisionError",
    "RuleRevisionResult",
    "RuleWorkbench",
    "RuleWorkbenchPolicy",
    "YaraEnginePort",
    "YaraSampleInput",
]

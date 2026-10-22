from __future__ import annotations

import hashlib
import socket
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisResult,
)
from cti_app.application.investigation_persistence import DetectionCandidateRepository
from cti_app.application.rule_workbench import (
    RuleCorpusBytesUnavailable,
    RuleValidationReason,
    RuleWorkbench,
    YaraSampleInput,
    rule_validation_reason,
    validate_single_rule_candidate,
)
from cti_app.domain.goodware import (
    Banality,
    DeclaredNonDiscriminant,
    GoodwareFeature,
    NonDiscriminantPatternRegistry,
)
from cti_app.domain.invariants import (
    CandidateInvariant,
    InvariantCategory,
    InvariantType,
    SampleFeatureProvenance,
)
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    ToolInvocationStatus,
)
from cti_app.domain.legitimate_services import (
    LegitimateServiceCategory,
    LegitimateServiceRegistry,
    LegitimateServiceV1,
)
from cti_app.domain.reference_corpus import ReferenceCorpusVerdict
from cti_app.domain.rule_workbench import (
    RuleConditionStrategy,
    RuleMotif,
    RuleMotifIssueCode,
    RuleMotifKind,
    RuleMotifProvenanceKind,
    RuleSampleProvenance,
    RuleTestCase,
    RuleTestCorpusRole,
    RuleTestErrorCode,
    RuleValidationStatus,
    YaraEngineResult,
    YaraExecutionStatus,
    detect_file_format_magic,
    wilson_rate,
)
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class MemoryCorpusBytes:
    def __init__(self, samples: dict[str, bytes]) -> None:
        self.samples = samples

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        try:
            return self.samples[sample_sha256]
        except KeyError as exc:
            raise RuleCorpusBytesUnavailable(sample_sha256) from exc


class MemoryDetectionCandidates(DetectionCandidateRepository):
    def __init__(self) -> None:
        self.versions: dict[UUID, list[DetectionCandidateVersion]] = {}

    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None:
        return next(
            (
                version
                for versions in self.versions.values()
                for version in versions
                if version.id == version_id
            ),
            None,
        )

    async def list_for_candidate(self, candidate_id: UUID) -> tuple[DetectionCandidateVersion, ...]:
        return tuple(self.versions.get(candidate_id, ()))

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]:
        return tuple(
            version
            for versions in self.versions.values()
            for version in versions
            if version.investigation_id == investigation_id
        )

    async def append_version(
        self,
        version: DetectionCandidateVersion,
        *,
        expected_previous_id: UUID | None,
    ) -> DetectionCandidateVersion:
        versions = self.versions.setdefault(version.candidate_id, [])
        actual_previous_id = versions[-1].id if versions else None
        if expected_previous_id != actual_previous_id:
            raise ValueError("version parent changed")
        if version.parent_version_id != expected_previous_id:
            raise ValueError("version parent does not match")
        versions.append(version)
        return version


class UnavailableYaraEngine:
    def compile(self, source: str) -> YaraEngineResult:
        del source
        return YaraEngineResult(
            status=YaraExecutionStatus.ENGINE_UNAVAILABLE,
            engine_version="unavailable",
            message="simulated unavailable engine",
        )

    def match(self, source: str, samples: Sequence[YaraSampleInput]) -> YaraEngineResult:
        raise AssertionError("match must not run when compilation is unavailable")


def _motif(value: str, *, observation_ref: UUID | None = None) -> RuleMotif:
    return RuleMotif(
        kind=RuleMotifKind.STRING,
        value=value,
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id=str(uuid4()),
        observation_refs=(observation_ref or uuid4(),),
    )


def _case(data: bytes, role: RuleTestCorpusRole) -> RuleTestCase:
    return RuleTestCase(
        sample_sha256=_sha256(data),
        role=role,
        holdout_designated_at=(
            datetime(2026, 1, 1, tzinfo=UTC)
            if role in {RuleTestCorpusRole.HOLDOUT_POSITIVE, RuleTestCorpusRole.HOLDOUT_NEGATIVE}
            else None
        ),
    )


def _workbench(
    samples: dict[str, bytes],
    *,
    repo: MemoryDetectionCandidates | None = None,
    engine: object | None = None,
    non_discriminant: NonDiscriminantPatternRegistry | None = None,
    legitimate: LegitimateServiceRegistry | None = None,
) -> RuleWorkbench:
    return RuleWorkbench(
        corpus_bytes=MemoryCorpusBytes(samples),
        yara_engine=engine or IsolatedYaraRunner(),  # type: ignore[arg-type]
        detection_candidates=repo or MemoryDetectionCandidates(),
        non_discriminant_patterns=non_discriminant or NonDiscriminantPatternRegistry(()),
        legitimate_services=legitimate or LegitimateServiceRegistry(()),
    )


@pytest.mark.parametrize(
    ("source", "expected_reason"),
    (
        (
            'rule first { strings: $a = "marker" condition: $a } '
            'rule second { strings: $b = "marker" condition: $b }',
            "exactly_one_rule_required",
        ),
        (
            'rule hash_gate { strings: $a = "marker" '
            'condition: hash.sha256(0, filesize) == "' + ("a" * 64) + '" }',
            "hash_equality_condition_forbidden",
        ),
        (
            'rule size_gate { strings: $a = "marker" condition: filesize == 1234 }',
            "filesize_only_condition_forbidden",
        ),
        (
            'rule branch_gate { strings: $a = "marker" $b = "other" condition: $a or $b }',
            "or_branches_disguised_as_multiple_rules_forbidden",
        ),
        (
            'import "dotnet" rule clr_only { condition: dotnet.is_assembly }',
            "generic_dotnet_import_only_forbidden",
        ),
    ),
)
def test_single_rule_source_guard_rejects_cheating_shapes(
    source: str, expected_reason: str
) -> None:
    assert expected_reason in validate_single_rule_candidate(source)


def test_single_rule_source_guard_accepts_one_measured_rule_using_all_of_them() -> None:
    source = 'rule measured { strings: $a = "marker" condition: 1 of them }'
    assert validate_single_rule_candidate(source) == ()


@pytest.mark.asyncio
async def test_rule_coverage_matrix_includes_actual_format_and_literal_offsets() -> None:
    data = b"prefix:RareMarkerQz:suffix"
    source = 'rule measured { strings: $marker = "RareMarkerQz" condition: $marker }'
    report = await _workbench({_sha256(data): data}).compile_and_test(
        source,
        (_case(data, RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED),),
        selection_bias="One selected candidate sample.",
        coverage_statement="One candidate's bytes were measured.",
    )
    role = next(
        item for item in report.roles if item.role is RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED
    )
    assert role.test_cases[0].expected_result is None
    assert role.file_results[0].matched is True
    assert role.file_results[0].format_magic == "text"
    assert role.file_results[0].strings[0].identifier == "$marker"
    assert role.file_results[0].strings[0].offsets == (7,)
    assert detect_file_format_magic(b"MZ" + b"\\x00" * 64) == "dos_mz"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_status",
    (
        YaraExecutionStatus.ENGINE_CRASHED,
        YaraExecutionStatus.TIMEOUT,
        YaraExecutionStatus.RESOURCE_EXCEEDED,
    ),
)
async def test_confined_rule_test_marks_one_failed_file_non_measurable(
    file_status: YaraExecutionStatus,
) -> None:
    problematic = b"one file that the isolated worker cannot measure"
    goodware = b"a separate measurable file"
    problematic_sha256 = _sha256(problematic)
    goodware_sha256 = _sha256(goodware)
    artifacts = {problematic_sha256: uuid4(), goodware_sha256: uuid4()}
    investigation_id = uuid4()

    class CorpusArtifactIds:
        async def get_artifact_id_for_sample(self, sample_sha256: str) -> UUID:
            return artifacts[sample_sha256]

    class ConfinedYara:
        family_statuses: ClassVar[dict[str, ConfinedAnalysisFamilyStatus]] = {
            "yara": ConfinedAnalysisFamilyStatus("yara", True, "4.5.0")
        }

        async def execute(self, request: Any) -> ConfinedAnalysisResult:
            status = file_status.value if request.sha256 == problematic_sha256 else "ok"
            message = "per-file YARA limit reached" if request.sha256 == problematic_sha256 else ""
            return ConfinedAnalysisResult(
                status=ToolInvocationStatus.SUCCEEDED,
                operation_key="confined-analysis:test",
                artifact_id=request.artifact_id,
                input_sha256=request.sha256,
                payload={
                    "result": {
                        "status": status,
                        "version": "4.5.0",
                        "matches": False,
                        "message": message,
                    }
                },
            )

    workbench = RuleWorkbench(
        corpus_bytes=CorpusArtifactIds(),  # type: ignore[arg-type]
        yara_engine=None,
        confined_analysis=ConfinedYara(),  # type: ignore[arg-type]
        detection_candidates=MemoryDetectionCandidates(),
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )

    report = await workbench.compile_and_test(
        'rule harmless { strings: $marker = "marker" condition: $marker }',
        (
            RuleTestCase(
                sample_sha256=problematic_sha256,
                role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
            ),
            RuleTestCase(sample_sha256=goodware_sha256, role=RuleTestCorpusRole.GOODWARE),
        ),
        investigation_id=investigation_id,
        selection_bias="One isolated-file failure is paired with measurable goodware.",
        coverage_statement="The worker measured one file and could not measure one file.",
    )

    positive = next(
        role for role in report.roles if role.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
    )
    assert report.execution_status is YaraExecutionStatus.OK
    assert positive.non_measurable == (problematic_sha256,)
    assert positive.errors[0].code.value == file_status.value
    assert positive.errors[0].message == "per-file YARA limit reached"


@pytest.mark.asyncio
async def test_confined_rule_test_resolves_worker_bytes_for_every_operator_corpus_role() -> None:
    roles = (
        RuleTestCorpusRole.DOCUMENTED_SEED_UNCONFIRMED,
        RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED,
        RuleTestCorpusRole.HUMAN_CONFIRMED,
        RuleTestCorpusRole.GOODWARE,
        RuleTestCorpusRole.HARD_NEGATIVE,
        RuleTestCorpusRole.HOLDOUT_POSITIVE,
        RuleTestCorpusRole.HOLDOUT_NEGATIVE,
        RuleTestCorpusRole.OUT_OF_SCOPE,
    )
    hashes = tuple(f"{index + 1:064x}" for index in range(len(roles)))
    artifacts = {sample_sha256: uuid4() for sample_sha256 in hashes}
    investigation_id = uuid4()

    class CorpusArtifactIds:
        byte_reads = 0

        async def get_artifact_id_for_sample(self, sample_sha256: str) -> UUID:
            return artifacts[sample_sha256]

        async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
            self.byte_reads += 1
            raise AssertionError(f"application process read corpus bytes for {sample_sha256}")

    class ConfinedYara:
        family_statuses: ClassVar[dict[str, ConfinedAnalysisFamilyStatus]] = {
            "yara": ConfinedAnalysisFamilyStatus("yara", True, "4.5.0")
        }

        def __init__(self) -> None:
            self.requests = []

        async def execute(self, request: Any) -> ConfinedAnalysisResult:
            self.requests.append(request)
            return ConfinedAnalysisResult(
                status=ToolInvocationStatus.SUCCEEDED,
                operation_key="confined-analysis:all-corpus-roles",
                artifact_id=request.artifact_id,
                input_sha256=request.sha256,
                payload={
                    "result": {
                        "status": "ok",
                        "version": "4.5.0",
                        "matches": False,
                        "format_magic": "text",
                        "string_hits": [{"identifier": "$marker", "offsets": [0]}],
                        "unavailable_strings": [],
                        "coverage_details_complete": True,
                    }
                },
            )

    corpus_bytes = CorpusArtifactIds()
    confined = ConfinedYara()
    workbench = RuleWorkbench(
        corpus_bytes=corpus_bytes,  # type: ignore[arg-type]
        yara_engine=None,
        confined_analysis=confined,  # type: ignore[arg-type]
        detection_candidates=MemoryDetectionCandidates(),
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )
    designation = datetime(2026, 1, 1, tzinfo=UTC)
    cases = tuple(
        RuleTestCase(
            sample_sha256=sample_sha256,
            role=role,
            holdout_designated_at=(designation if role.value.startswith("holdout_") else None),
        )
        for role, sample_sha256 in zip(roles, hashes, strict=True)
    )

    rule_source = " " * 300_000 + 'rule harmless { strings: $marker = "marker" condition: $marker }'
    report = await workbench.compile_and_test(
        rule_source,
        cases,
        investigation_id=investigation_id,
        selection_bias="Operator roles use canonical quarantine artifact identities.",
        coverage_statement="The confined worker measures bytes for each corpus role.",
    )

    assert report.execution_status is YaraExecutionStatus.OK
    assert corpus_bytes.byte_reads == 0
    assert {request.sha256 for request in confined.requests} == set(hashes)
    assert all(request.artifact_id == artifacts[request.sha256] for request in confined.requests)
    assert all(request.artifact_kind.value == "corpus_sample" for request in confined.requests)
    assert all(request.tool.value == "yara" for request in confined.requests)
    assert all(
        request.analysis_parameters["rule_source"] == rule_source for request in confined.requests
    )
    assert {role.role for role in report.roles if role.test_cases} == set(roles)


@pytest.mark.asyncio
async def test_versions_retest_on_expanded_corpus_and_compare_matches() -> None:
    initial_bytes = b"safe-fixture:Qz9Vn5Hs:tail"
    expanded_bytes = b"synthetic-candidate:Ty7Kp3Wq:end"
    goodware_bytes = b"ordinary synthetic goodware content"
    samples = {
        _sha256(initial_bytes): initial_bytes,
        _sha256(expanded_bytes): expanded_bytes,
        _sha256(goodware_bytes): goodware_bytes,
    }
    repository = MemoryDetectionCandidates()
    workbench = _workbench(samples, repo=repository)
    first_motif = _motif("Qz9Vn5Hs")
    v1 = await workbench.revise(
        None,
        new_motifs=(first_motif,),
        new_condition=RuleConditionStrategy.ANY,
        investigation_id=uuid4(),
        corpus=(
            _case(initial_bytes, RuleTestCorpusRole.CONFIRMED_POSITIVE),
            _case(goodware_bytes, RuleTestCorpusRole.GOODWARE),
        ),
        selection_bias="Seeded with one analyst-confirmed synthetic positive.",
        coverage_statement="One positive and one synthetic goodware sample were measurable.",
    )
    assert v1.status is DetectionCandidateStatus.TESTED
    with pytest.raises(FrozenInstanceError):
        v1.__setattr__("source", "changed")

    second_motif = RuleMotif(
        kind=RuleMotifKind.STRING,
        value="Ty7Kp3Wq",
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id=str(uuid4()),
        observation_refs=(uuid4(),),
        sample_sha256=_sha256(expanded_bytes),
        offsets=(expanded_bytes.index(b"Ty7Kp3Wq"),),
        positive_support=1,
    )
    expanded_corpus = (
        _case(initial_bytes, RuleTestCorpusRole.CONFIRMED_POSITIVE),
        _case(expanded_bytes, RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE),
        _case(goodware_bytes, RuleTestCorpusRole.GOODWARE),
    )
    v2 = await workbench.revise(
        v1,
        new_motifs=(first_motif, second_motif),
        corpus=expanded_corpus,
        selection_bias="The newly added candidate was selected for investigation.",
        coverage_statement=(
            "Two synthetic candidates and one synthetic goodware sample were measured."
        ),
    )
    comparison = await workbench.compare(
        v1,
        v2,
        expanded_corpus,
        selection_bias="The corpus contains selected synthetic examples.",
        coverage_statement="All three synthetic samples were resolved locally.",
    )

    assert v2.version == 2
    assert v2.parent_version_id == v1.id
    assert v2.source != v1.source
    assert v2.source_sha256 != v1.source_sha256
    assert v2.status is DetectionCandidateStatus.TESTED
    out_of_cluster = next(
        role
        for role in comparison.roles
        if role.role is RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE
    )
    assert out_of_cluster.matches_gained == (_sha256(expanded_bytes),)
    assert comparison.false_positives_added == ()
    assert tuple(repository.versions[v1.candidate_id]) == (v1, v2)


@pytest.mark.asyncio
async def test_noisy_rule_is_rejected_and_false_positives_remain_visible() -> None:
    positive = b"synthetic-positive:Qz9Vn5Hs"
    goodware = b"synthetic-goodware:Qz9Vn5Hs"
    samples = {_sha256(positive): positive, _sha256(goodware): goodware}
    workbench = _workbench(samples)
    motif = _motif("Qz9Vn5Hs")
    corpus = (
        _case(positive, RuleTestCorpusRole.CONFIRMED_POSITIVE),
        _case(goodware, RuleTestCorpusRole.GOODWARE),
    )
    built = workbench.build_rule((motif,), RuleConditionStrategy.ANY)
    assert built.source is not None
    revision = await workbench.revise_with_report(
        None,
        new_motifs=(motif,),
        new_condition=RuleConditionStrategy.ANY,
        investigation_id=uuid4(),
        corpus=corpus,
        selection_bias="Synthetic fixture deliberately shares a motif.",
        coverage_statement="One positive and one goodware sample were measured.",
    )
    report = revision.test_report
    version = revision.version

    goodware_result = next(
        role for role in report.roles if role.role is RuleTestCorpusRole.GOODWARE
    )
    assert goodware_result.matches == (_sha256(goodware),)
    assert version.status is DetectionCandidateStatus.REJECTED
    assert workbench.build_rule((motif,), RuleConditionStrategy.ANY).valid


def test_motif_validation_reports_length_entropy_and_known_noise() -> None:
    non_discriminant = NonDiscriminantPatternRegistry(
        (
            DeclaredNonDiscriminant(
                category="msvc_crt",
                feature_kind="string",
                normalized_value="Qz9Vn5Hs".lower(),
            ),
        )
    )
    legitimate = LegitimateServiceRegistry(
        (
            LegitimateServiceV1(
                hostname="benign.example",
                category=LegitimateServiceCategory.MESSAGING,
                reason="synthetic test service",
            ),
        )
    )
    workbench = _workbench({}, non_discriminant=non_discriminant, legitimate=legitimate)
    report = workbench.validate_motifs(
        (
            _motif("abc"),
            _motif("AAAAAAAA"),
            _motif("Qz9Vn5Hs"),
            _motif("benign.example"),
        )
    )
    codes = {issue.code for issue in report.issues}
    assert RuleMotifIssueCode.TOO_SHORT in codes
    assert RuleMotifIssueCode.LOW_ENTROPY in codes
    assert RuleMotifIssueCode.KNOWN_NON_DISCRIMINANT in codes


def test_rule_source_escapes_metadata_and_uses_safe_deterministic_identity() -> None:
    workbench = _workbench({})
    motif = _motif("Qz9Vn5Hs")
    metadata = {"description": 'safe "text"\\\nrule injected { condition: true }'}

    first = workbench.build_rule((motif,), RuleConditionStrategy.ANY, metadata)
    second = workbench.build_rule((motif,), RuleConditionStrategy.ANY, metadata)

    assert first.valid
    assert not first.accepted
    assert first.rule_identifier is not None
    assert first.rule_identifier.startswith("autowork_")
    assert first.source == second.source
    assert first.source is not None and "\\x0A" in first.source
    assert IsolatedYaraRunner().compile(first.source).status is YaraExecutionStatus.OK
    invalid = workbench.build_rule((motif,), RuleConditionStrategy.ANY, {"bad key": "value"})
    assert invalid.source is None
    assert invalid.validation.issues[0].code is RuleMotifIssueCode.INVALID_METADATA


def test_feature_proposals_keep_p09_identity_offsets_and_goodware_counts() -> None:
    data = b"synthetic feature data"
    sample_sha256 = _sha256(data)
    invariant = CandidateInvariant(
        investigation_id=uuid4(),
        type=InvariantType.LITERAL_STRING,
        category=InvariantCategory.C2_INDICATOR,
        pattern="Qz9Vn5Hs",
        proposal_key="a" * 64,
        provenances=(
            SampleFeatureProvenance(
                sample_sha256=sample_sha256,
                feature_id="sample-feature-set-1",
                offsets=(12, 48),
            ),
        ),
        banality=Banality.SPECIFIC,
        corpus_verdict=ReferenceCorpusVerdict.FAMILY_SPECIFIC,
        positive_support=2,
        benign_prevalence=1,
    )
    workbench = _workbench({})
    refs = (uuid4(),)
    motifs = workbench.propose_from_features(
        (invariant,),
        observation_refs_by_invariant={invariant.id: refs},
        goodware_features=(
            GoodwareFeature(
                feature_kind="string",
                normalized_value="Qz9Vn5Hs".lower(),
                occurrence_count=3,
            ),
        ),
    )
    assert len(motifs) == 1
    assert motifs[0].provenance_kind is RuleMotifProvenanceKind.P09_INVARIANT
    assert motifs[0].provenance_id == str(invariant.id)
    assert motifs[0].offsets == (12, 48)
    assert motifs[0].sample_sha256 == sample_sha256
    assert motifs[0].observation_refs == refs
    assert motifs[0].positive_support == 2
    assert motifs[0].goodware_occurrence_count == 3


@pytest.mark.asyncio
async def test_non_measurable_cases_stay_separate_from_non_matches() -> None:
    data = b"synthetic:Qz9Vn5Hs"
    workbench = _workbench({})
    built = workbench.build_rule((_motif("Qz9Vn5Hs"),), RuleConditionStrategy.ANY)
    assert built.source is not None
    report = await workbench.compile_and_test(
        built.source,
        (_case(data, RuleTestCorpusRole.CONFIRMED_POSITIVE),),
        selection_bias="The sample is a selected synthetic fixture.",
        coverage_statement="The quarantined bytes were intentionally unavailable.",
    )
    positive = next(
        role for role in report.roles if role.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
    )
    assert positive.non_measurable == (_sha256(data),)
    assert positive.non_matches == ()
    assert positive.matches == ()
    assert positive.corpus_sha256s == (_sha256(data),)


@pytest.mark.asyncio
async def test_compile_errors_are_typed_and_engine_unavailable_is_local() -> None:
    data = b"synthetic:Qz9Vn5Hs"
    case = _case(data, RuleTestCorpusRole.CONFIRMED_POSITIVE)
    workbench = _workbench({_sha256(data): data})
    compile_report = await workbench.compile_and_test(
        'rule broken { strings: $marker = "marker" condition: $marker',
        (case,),
        selection_bias="One synthetic selected sample.",
        coverage_statement="One corpus member was declared.",
    )
    positive_errors = next(
        role.errors
        for role in compile_report.roles
        if role.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
    )
    assert compile_report.execution_status is YaraExecutionStatus.COMPILE_ERROR
    assert positive_errors and positive_errors[0].code is RuleTestErrorCode.COMPILE_ERROR
    assert positive_errors[0].line is not None

    unavailable = _workbench(
        {_sha256(data): data},
        engine=UnavailableYaraEngine(),
    )
    built = unavailable.build_rule((_motif("Qz9Vn5Hs"),), RuleConditionStrategy.ANY)
    assert built.source is not None
    unavailable_report = await unavailable.compile_and_test(
        built.source,
        (case,),
        selection_bias="One synthetic selected sample.",
        coverage_statement="One corpus member was declared.",
    )
    assert unavailable_report.execution_status is YaraExecutionStatus.ENGINE_UNAVAILABLE
    assert (
        next(
            role.errors
            for role in unavailable_report.roles
            if role.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
        )[0].code
        is RuleTestErrorCode.ENGINE_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_validation_status_and_reason_are_stable_and_honest() -> None:
    positive = b"AUTOWORK_RULE_VALIDATION_FIXTURE"
    goodware = b"ordinary harmless content"
    source = (
        "rule autowork_validation_fixture { strings: $marker = "
        '"AUTOWORK_RULE_VALIDATION_FIXTURE" condition: $marker }'
    )
    positive_hash = _sha256(positive)
    goodware_hash = _sha256(goodware)
    workbench = _workbench({positive_hash: positive, goodware_hash: goodware})

    empty = await workbench.compile_and_test(
        source,
        (),
        selection_bias="No samples were imported.",
        coverage_statement="The validation corpus is empty.",
    )
    assert empty.validation_status is RuleValidationStatus.UNTESTED
    assert rule_validation_reason(empty) is RuleValidationReason.CORPUS_EMPTY

    synthetic = await workbench.compile_and_test(
        source,
        (
            RuleTestCase(
                sample_sha256=positive_hash,
                role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
                provenance=RuleSampleProvenance.SYNTHETIC,
            ),
            RuleTestCase(
                sample_sha256=goodware_hash,
                role=RuleTestCorpusRole.GOODWARE,
                provenance=RuleSampleProvenance.SYNTHETIC,
            ),
        ),
        selection_bias="The fixtures are synthetic.",
        coverage_statement="Only synthetic positive and goodware bytes were measured.",
    )
    assert synthetic.validation_status is RuleValidationStatus.SYNTHETIC_ONLY
    assert rule_validation_reason(synthetic) is RuleValidationReason.SYNTHETIC_POSITIVES

    missing_bytes = await _workbench({}).compile_and_test(
        source,
        (
            RuleTestCase(
                sample_sha256=positive_hash,
                role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
            ),
            RuleTestCase(
                sample_sha256=goodware_hash,
                role=RuleTestCorpusRole.GOODWARE,
            ),
        ),
        selection_bias="Both declared samples are canonical real records.",
        coverage_statement="Neither referenced byte stream is available for measurement.",
    )
    assert missing_bytes.validation_status is RuleValidationStatus.UNTESTED
    assert rule_validation_reason(missing_bytes) is RuleValidationReason.SAMPLE_BYTES_MISSING


@pytest.mark.asyncio
async def test_zero_observed_false_positives_keeps_wide_interval_and_unknown_sensitivity() -> None:
    samples = {
        _sha256(data): data
        for data in (f"goodware fixture {index:02d}".encode() for index in range(12))
    }
    cases = tuple(
        RuleTestCase(
            sample_sha256=sample_sha256,
            role=RuleTestCorpusRole.GOODWARE,
            provenance=RuleSampleProvenance.SYNTHETIC,
            expected_match=False,
        )
        for sample_sha256 in sorted(samples)
    )
    report = await _workbench(samples).compile_and_test(
        'rule no_match { strings: $never = "never-match-this-corpus" condition: $never }',
        cases,
        selection_bias="Twelve generated unit-test examples, not real DLLs.",
        coverage_statement="Only synthetic examples were measured.",
    )

    metrics = report.metrics
    fpr = metrics["real_goodware_false_positive_rate"]
    sensitivity = metrics["real_positive_sensitivity"]
    assert fpr["status"] == "not_calculable"
    assert sensitivity["status"] == "not_calculable"
    assert report.validation_status is RuleValidationStatus.SYNTHETIC_ONLY

    zero_of_twelve = wilson_rate(successes=0, trials=12)
    interval = zero_of_twelve["confidence_interval_95"]
    assert isinstance(interval, dict)
    assert interval["lower"] == 0
    assert interval["upper"] == pytest.approx(0.2425, abs=0.0001)


def test_common_dotnet_import_needs_measured_conjunction() -> None:
    workbench = _workbench({})
    source = b"System.Runtime.InteropServices prefix:Qz9Vn5Hs:suffix"
    source_hash = _sha256(source)
    generic = RuleMotif(
        kind=RuleMotifKind.STRING,
        value="System.Runtime.InteropServices",
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id="common-managed-import",
        sample_sha256=source_hash,
        offsets=(source.index(b"System.Runtime.InteropServices"),),
        observation_refs=(uuid4(),),
    )
    specific = RuleMotif(
        kind=RuleMotifKind.STRING,
        value="Qz9Vn5Hs",
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id="fixture-specific-byte-motif",
        sample_sha256=source_hash,
        offsets=(source.index(b"Qz9Vn5Hs"),),
        observation_refs=(uuid4(),),
    )
    meta = {
        "hypothesis": "A family-specific payload is identified by its private marker.",
        "dotnet_imports_discussion": (
            "The CLR namespace is common in benign .NET software and is context only."
        ),
    }

    import_only = workbench.validate_motifs(
        (generic,), meta=meta, condition=RuleConditionStrategy.ALL
    )
    broad_any = workbench.validate_motifs(
        (generic, specific), meta=meta, condition=RuleConditionStrategy.ANY
    )
    narrowed_all = workbench.validate_motifs(
        (generic, specific), meta=meta, condition=RuleConditionStrategy.ALL
    )

    assert any(
        issue.code is RuleMotifIssueCode.GENERIC_DOTNET_IMPORT for issue in import_only.issues
    )
    assert any(issue.code is RuleMotifIssueCode.GENERIC_DOTNET_IMPORT for issue in broad_any.issues)
    assert narrowed_all.valid


@pytest.mark.asyncio
async def test_common_dotnet_import_source_hash_and_offset_are_checked_on_bytes() -> None:
    source = b"System.IO managed assembly marker Qz9Vn5Hs"
    sample_sha256 = _sha256(source)
    generic = RuleMotif(
        kind=RuleMotifKind.STRING,
        value="System.IO",
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id="common-dotnet-import",
        sample_sha256=sample_sha256,
        offsets=(source.index(b"System.IO"),),
        observation_refs=(uuid4(),),
    )
    specific = RuleMotif(
        kind=RuleMotifKind.STRING,
        value="Qz9Vn5Hs",
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id="measured-marker",
        sample_sha256=sample_sha256,
        offsets=(source.index(b"Qz9Vn5Hs"),),
        observation_refs=(uuid4(),),
    )
    workbench = _workbench({sample_sha256: source})

    verified = await workbench.verify_dotnet_source_motifs((generic, specific))
    invalid_offset = RuleMotif(
        kind=RuleMotifKind.STRING,
        value="Qz9Vn5Hs",
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id="measured-marker",
        sample_sha256=sample_sha256,
        offsets=(0,),
        observation_refs=(uuid4(),),
    )
    rejected = await workbench.verify_dotnet_source_motifs((generic, invalid_offset))

    assert verified.valid
    assert any(
        issue.code is RuleMotifIssueCode.SOURCE_BYTES_UNVERIFIED for issue in rejected.issues
    )


@pytest.mark.asyncio
async def test_non_dotnet_bridge_feature_is_outside_dotnet_source_guard() -> None:
    motif = RuleMotif(
        kind=RuleMotifKind.STRING,
        value="Qz9Vn5Hs",
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id="similarity-feature-p09-invariant",
        sample_sha256="a" * 64,
        offsets=(16,),
        observation_refs=(uuid4(),),
    )

    report = await _workbench({}).verify_dotnet_source_motifs((motif,))

    assert report.valid
    assert report.verified_motif_indices == ()


@pytest.mark.asyncio
async def test_rule_workbench_does_not_open_network_sockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"synthetic:Qz9Vn5Hs"
    workbench = _workbench({_sha256(data): data})
    built = workbench.build_rule((_motif("Qz9Vn5Hs"),), RuleConditionStrategy.ANY)
    assert built.source is not None

    def fail_socket(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access is forbidden in this test")

    monkeypatch.setattr(socket, "socket", fail_socket)
    report = await workbench.compile_and_test(
        built.source,
        (_case(data, RuleTestCorpusRole.CONFIRMED_POSITIVE),),
        selection_bias="One synthetic selected sample.",
        coverage_statement="The local worker handles the only corpus member.",
    )
    assert report.execution_status is YaraExecutionStatus.OK

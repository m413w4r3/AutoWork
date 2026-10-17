from __future__ import annotations

import hashlib
import socket
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_persistence import DetectionCandidateRepository
from cti_app.application.rule_workbench import (
    RuleCorpusBytesUnavailable,
    RuleWorkbench,
    YaraSampleInput,
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
    RuleTestCase,
    RuleTestCorpusRole,
    RuleTestErrorCode,
    YaraEngineResult,
    YaraExecutionStatus,
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
    return RuleTestCase(sample_sha256=_sha256(data), role=role)


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
        offsets=(19,),
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
        "rule broken {",
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

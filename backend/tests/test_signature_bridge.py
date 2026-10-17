from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.invariants import InvariantProposalResult, InvariantRegistryService
from cti_app.application.investigation_rule_tools import register_rule_tools
from cti_app.application.investigation_signature_bridge_tools import (
    register_signature_bridge_tools,
    signature_bridge_tool_capabilities,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolExecutionContext,
)
from cti_app.application.rule_workbench import (
    RuleCorpusBytesUnavailable,
    RuleWorkbench,
)
from cti_app.application.sample_similarity import StoredFamilyFeatureComparison
from cti_app.domain.goodware import NonDiscriminantPatternRegistry
from cti_app.domain.invariants import (
    CandidateInvariant,
    FeatureMeasurements,
    ResolvedFeature,
    SampleFeatureProvenance,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    InvestigationBudget,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.rule_workbench import (
    RuleConditionStrategy,
    RuleCorpusInventoryItem,
    RuleMotif,
    RuleMotifIssueCode,
    RuleMotifKind,
    RuleMotifProvenanceKind,
    RuleSampleProvenance,
    RuleTestCase,
    RuleTestCorpusRole,
)
from cti_app.domain.sample_similarity import (
    FamilyFeatureClass,
    FamilyFeatureComparisonV1,
    FeatureComparison,
    FeatureRejectionReason,
    FeatureSource,
    SampleFeaturePresence,
    SignatureCandidate,
    SignatureMotifKind,
    SimilarityFeatureKind,
    SimilarityLimits,
    SimilarityPolicy,
    SimilarityStatus,
)
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner

_INVESTIGATION_ID = UUID("1a602d9c-f9a8-45a8-94c7-0a99df1c79e1")
_A = hashlib.sha256(b"sample-a").hexdigest()
_B = hashlib.sha256(b"sample-b").hexdigest()
_C = hashlib.sha256(b"sample-c").hexdigest()
_FEATURE_KEY = hashlib.sha256(b"signature feature").hexdigest()


class _FakeSimilarity:
    def __init__(self, comparison: FamilyFeatureComparisonV1) -> None:
        self.comparison = comparison

    async def compare(
        self, *, investigation_id: UUID, sample_sha256s: Sequence[str]
    ) -> StoredFamilyFeatureComparison:
        assert investigation_id == _INVESTIGATION_ID
        assert set(sample_sha256s) == set(self.comparison.comparison_sample_sha256s)
        return StoredFamilyFeatureComparison(
            comparison=self.comparison,
            artifact_blob_id=uuid4(),
            artifact_sha256=self.comparison.sha256,
            artifact_size=len(self.comparison.canonical_bytes),
        )


class _FakeAssociations:
    def __init__(self, sample_hashes: Sequence[str]) -> None:
        self.sample_hashes = tuple(sample_hashes)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        assert investigation_id == _INVESTIGATION_ID
        return tuple(SimpleNamespace(sample_sha256=item) for item in self.sample_hashes)


class _FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.observation_id = uuid4()
        self.json_observations: dict[str, Any] = {}

    async def record_existing_blob(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(id=self.observation_id)

    async def record_json(self, **kwargs: Any) -> Any:
        key = str(kwargs.get("idempotency_key") or uuid4())
        if key not in self.json_observations:
            self.json_observations[key] = SimpleNamespace(
                id=uuid4(),
                kind=kwargs["kind"],
                object_type=kwargs["object_type"],
                object_id=kwargs["object_id"],
                blob_id=uuid4(),
            )
        return self.json_observations[key]


class _FakeUow:
    def __init__(self, sample_hashes: Sequence[str]) -> None:
        self.analyst_investigations = SimpleNamespace(
            get=self._investigation,
        )
        self.investigation_goodware_baselines = SimpleNamespace(get=self._no_baseline)
        self.samples = SimpleNamespace(list_for_subject=self._samples)
        self.blobs = SimpleNamespace(get=self._blob)
        self.invariants: Any = None
        self.subject_id = uuid4()
        self.samples_by_blob = {uuid4(): sample_hash for sample_hash in sample_hashes}
        self.sample_rows = [
            SimpleNamespace(id=uuid4(), blob_id=blob_id) for blob_id in self.samples_by_blob
        ]

    async def _investigation(self, investigation_id: UUID) -> Any:
        assert investigation_id == _INVESTIGATION_ID
        return SimpleNamespace(subject_id=self.subject_id, cycle_number=1)

    async def _no_baseline(self, investigation_id: UUID) -> None:
        assert investigation_id == _INVESTIGATION_ID
        return None

    async def _samples(self, subject_id: UUID) -> Sequence[Any]:
        assert subject_id == self.subject_id
        return tuple(self.sample_rows)

    async def _blob(self, blob_id: UUID) -> Any:
        sample_hash = self.samples_by_blob[blob_id]
        return SimpleNamespace(descriptor=SimpleNamespace(sha256=sample_hash))

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _FakeUow:
        return self

    async def __aexit__(self, *args: Any) -> None:
        del args


class _FakeUowFactory:
    def __init__(self, sample_hashes: Sequence[str]) -> None:
        self.uow = _FakeUow(sample_hashes)

    def __call__(self) -> AbstractAsyncContextManager[Any]:
        return self.uow


class _FakeInvariantRegistry:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def propose(self, **kwargs: Any) -> InvariantProposalResult:
        self.calls.append(kwargs)
        invariant = CandidateInvariant(
            investigation_id=kwargs["investigation_id"],
            type=kwargs["type"],
            category=kwargs["category"],
            pattern=kwargs["pattern"],
            proposal_key=hashlib.sha256(kwargs["pattern"].encode()).hexdigest(),
            provenances=tuple(kwargs["provenances"]),
            positive_support=len(kwargs["sample_ids"]),
        )
        return InvariantProposalResult(invariant=invariant, rejection=None)


class _FakeRuleTools:
    signature_bridge_available = True

    def __init__(
        self, *, comparison_status: ToolInvocationStatus = ToolInvocationStatus.SUCCEEDED
    ) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.candidate_id = uuid4()
        self.comparison_status = comparison_status

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        del context
        self.calls.append((name, arguments))
        if name == "rule.propose":
            return (
                {
                    "candidate_id": str(self.candidate_id),
                    "version": "v1",
                    "candidate_status": "tested",
                    "observation_refs": [str(uuid4())],
                },
                ToolInvocationStatus.SUCCEEDED,
                (uuid4(),),
            )
        return (
            {"version_a": "v1", "version_b": "v2", "false_positives_added": []},
            self.comparison_status,
            (uuid4(),),
        )

    async def revise_with_added_motifs(
        self,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        del context
        self.calls.append(("rule.revise", arguments))
        return (
            {
                "candidate_id": arguments["candidate_id"],
                "version": f"v{arguments['parent_version'] + 1}",
                "candidate_status": "tested",
            },
            ToolInvocationStatus.SUCCEEDED,
            (uuid4(),),
        )


def _comparison(
    *,
    sample_hashes: Sequence[str],
    positive_hashes: Sequence[str],
    qualified: bool,
    rejection_reasons: tuple[FeatureRejectionReason, ...] = (),
    pattern: str = "Qz9Vn5Hs",
) -> FamilyFeatureComparisonV1:
    feature_set_ids = {sample_hash: uuid4() for sample_hash in positive_hashes}
    presences = tuple(
        SampleFeaturePresence(
            sample_sha256=sample_hash,
            present=sample_hash in positive_hashes,
            measured=True,
            selected=True,
            qualification="candidate",
            offsets=(16 + index,) if sample_hash in positive_hashes else (),
            sources=(
                FeatureSource(
                    source_kind="sample_feature_pack",
                    source_id=str(feature_set_ids[sample_hash]),
                    extractor_version="strings-v1",
                    offset=16 + index,
                ),
            )
            if sample_hash in positive_hashes
            else (),
        )
        for index, sample_hash in enumerate(sample_hashes)
    )
    candidate = SignatureCandidate(
        feature_key=_FEATURE_KEY,
        motif_kind=SignatureMotifKind.ASCII,
        pattern=pattern,
        qualified=qualified,
        positive_support=len(positive_hashes),
        positive_sample_sha256s=tuple(positive_hashes),
        source_offsets=tuple(item for item in presences if item.present),
        rejection_reasons=rejection_reasons,
    )
    feature = FeatureComparison(
        feature_key=_FEATURE_KEY,
        kind=SimilarityFeatureKind.STRING,
        value=pattern,
        encoding="ascii",
        feature_class=FamilyFeatureClass.CORE_STRONGLY_SHARED,
        rank=1,
        positive_support=len(positive_hashes),
        positive_sample_count=len(sample_hashes),
        corpus_support=len(positive_hashes),
        corpus_sample_count=len(sample_hashes),
        goodware_occurrence_count=0,
        comparison_exclusive=True,
        rejection_reasons=rejection_reasons,
        rationale=(),
        sample_presence=presences,
        signature_candidates=(candidate,),
    )
    return FamilyFeatureComparisonV1(
        investigation_id=_INVESTIGATION_ID,
        corpus_snapshot_sha256=hashlib.sha256(b"corpus").hexdigest(),
        comparison_sample_sha256s=tuple(sample_hashes),
        corpus_sample_sha256s=tuple(sample_hashes),
        status=SimilarityStatus.COMPLETE,
        policy=SimilarityPolicy(),
        limits=SimilarityLimits(
            selected_samples=len(sample_hashes),
            corpus_samples=len(sample_hashes),
            features=1,
        ),
        extractor_versions=(),
        features=(feature,),
        recommended_next_tests=(),
    )


def _bridge_context() -> ToolExecutionContext:
    budget = InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 600})
    return ToolExecutionContext(
        investigation_id=_INVESTIGATION_ID,
        envelope=None,
        budget=budget,
        idempotency_key="signature-bridge-test",
        available_capabilities=frozenset({"rule.propose_from_candidates"}),
    )


def _bridge_harness(
    comparison: FamilyFeatureComparisonV1,
) -> tuple[InMemoryToolRegistry, dict[str, Any]]:
    service = _FakeSimilarity(comparison)
    associations = _FakeAssociations(comparison.comparison_sample_sha256s)
    recorder = _FakeRecorder()
    uow_factory = _FakeUowFactory(comparison.comparison_sample_sha256s)
    invariant_registry = _FakeInvariantRegistry()
    rule_tools = _FakeRuleTools()
    registry = InMemoryToolRegistry()
    register_signature_bridge_tools(
        registry,
        service=service,  # type: ignore[arg-type]
        sample_associations=associations,  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        uow_factory=uow_factory,  # type: ignore[arg-type]
        invariant_registry=invariant_registry,  # type: ignore[arg-type]
        rule_tools=rule_tools,  # type: ignore[arg-type]
    )
    dependencies = {
        "service": service,
        "associations": associations,
        "recorder": recorder,
        "uow_factory": uow_factory,
        "invariant_registry": invariant_registry,
        "rule_tools": rule_tools,
    }
    return registry, dependencies


@pytest.mark.asyncio
async def test_bridge_records_p09_provenance_and_proposes_rule_v1() -> None:
    comparison = _comparison(sample_hashes=(_A, _B), positive_hashes=(_A, _B), qualified=True)
    registry, dependencies = _bridge_harness(comparison)
    assert signature_bridge_tool_capabilities(
        service=dependencies["service"],
        sample_associations=dependencies["associations"],
        recorder=dependencies["recorder"],
        uow_factory=dependencies["uow_factory"],
        invariant_registry=dependencies["invariant_registry"],
        rule_tools=dependencies["rule_tools"],
    ) == frozenset({"rule.propose_from_candidates"})
    registration = registry.get("rule.propose_from_candidates", "1")
    assert registration is not None
    _definition, adapter = registration

    result = await adapter.execute(
        {
            "sample_sha256s": [_A, _B],
            "feature_keys": [_FEATURE_KEY],
            "condition": "any",
        },
        _bridge_context(),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["sample_membership_changed"] is False
    assert result.output["p10_status"] == "not_invoked"
    assert "P10 n'est pas invoqué" in result.output["limitations"][0]
    assert "p10_input_invariant_ids" not in result.output
    invariant_call = dependencies["invariant_registry"].calls[0]
    assert invariant_call["positive_sample_confirmed"] is False
    assert len(invariant_call["provenances"]) == 2
    assert all(isinstance(item, SampleFeatureProvenance) for item in invariant_call["provenances"])
    rule_name, rule_arguments = dependencies["rule_tools"].calls[0]
    assert rule_name == "rule.propose"
    motif = rule_arguments["motifs"][0]
    assert motif["provenance_kind"] == RuleMotifProvenanceKind.P09_INVARIANT.value
    assert motif["observation_refs"] == [str(dependencies["recorder"].observation_id)]
    assert _FEATURE_KEY in motif["rationale"]
    assert len(result.observation_refs) == 2


@pytest.mark.asyncio
async def test_new_sample_proposes_v2_and_runs_existing_before_after_compare() -> None:
    comparison = _comparison(
        sample_hashes=(_A, _B, _C),
        positive_hashes=(_C,),
        qualified=False,
        rejection_reasons=(FeatureRejectionReason.INSUFFICIENT_POSITIVE_SUPPORT,),
        pattern="Ty7Kp3Wq",
    )
    registry, dependencies = _bridge_harness(comparison)
    registration = registry.get("rule.propose_from_candidates", "1")
    assert registration is not None
    _definition, adapter = registration

    result = await adapter.execute(
        {
            "sample_sha256s": [_A, _B, _C],
            "feature_keys": [_FEATURE_KEY],
            "condition": "any",
            "candidate_id": str(dependencies["rule_tools"].candidate_id),
            "parent_version": 1,
        },
        _bridge_context(),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["rule"]["version"] == "v2"
    assert result.output["before_after"]["version_a"] == "v1"
    assert result.output["before_after"]["version_b"] == "v2"
    assert result.output["sample_membership_changed"] is False
    names = [name for name, _arguments in dependencies["rule_tools"].calls]
    assert names == ["rule.revise", "rule.compare"]
    revise_args = dependencies["rule_tools"].calls[0][1]
    assert revise_args["motifs"][0]["sample_sha256"] == _C


@pytest.mark.asyncio
async def test_failed_required_v2_comparison_marks_bridge_partial() -> None:
    comparison = _comparison(
        sample_hashes=(_A, _B, _C),
        positive_hashes=(_C,),
        qualified=False,
        rejection_reasons=(FeatureRejectionReason.INSUFFICIENT_POSITIVE_SUPPORT,),
        pattern="Ty7Kp3Wq",
    )
    registry, dependencies = _bridge_harness(comparison)
    dependencies["rule_tools"].comparison_status = ToolInvocationStatus.FAILED
    registration = registry.get("rule.propose_from_candidates", "1")
    assert registration is not None
    _definition, adapter = registration

    result = await adapter.execute(
        {
            "sample_sha256s": [_A, _B, _C],
            "feature_keys": [_FEATURE_KEY],
            "condition": "any",
            "candidate_id": str(dependencies["rule_tools"].candidate_id),
            "parent_version": 1,
        },
        _bridge_context(),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["before_after_status"] == ToolInvocationStatus.FAILED.value


class _MemoryCorpus:
    def __init__(self, samples: Mapping[str, bytes]) -> None:
        self.samples = dict(samples)

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        try:
            return self.samples[sample_sha256]
        except KeyError as exc:
            raise RuleCorpusBytesUnavailable(sample_sha256) from exc


class _MemoryCandidateRepository:
    def __init__(self) -> None:
        self.versions: dict[UUID, list[DetectionCandidateVersion]] = {}

    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None:
        return next(
            (
                item
                for versions in self.versions.values()
                for item in versions
                if item.id == version_id
            ),
            None,
        )

    async def list_for_candidate(self, candidate_id: UUID) -> tuple[DetectionCandidateVersion, ...]:
        return tuple(self.versions.get(candidate_id, ()))

    async def append_version(
        self,
        version: DetectionCandidateVersion,
        *,
        expected_previous_id: UUID | None,
    ) -> DetectionCandidateVersion:
        versions = self.versions.setdefault(version.candidate_id, [])
        assert (versions[-1].id if versions else None) == expected_previous_id
        versions.append(version)
        return version


class _MemoryInvariantRepository:
    """Stateful P09 persistence port used with the real registry service."""

    def __init__(
        self,
        *,
        sample_hash_by_id: Mapping[UUID, str],
        positive_hashes_by_value: Mapping[str, set[str]],
    ) -> None:
        self.sample_hash_by_id = dict(sample_hash_by_id)
        self.positive_hashes_by_value = dict(positive_hashes_by_value)
        self.invariants_by_key: dict[str, CandidateInvariant] = {}

    async def lock_proposal(self, proposal_key: str) -> None:
        del proposal_key

    async def get_proposal_outcome(
        self, proposal_key: str
    ) -> tuple[CandidateInvariant | None, None]:
        return self.invariants_by_key.get(proposal_key), None

    async def resolve_provenance(
        self,
        *,
        provenance: SampleFeatureProvenance,
        invariant_type: Any,
        pattern: str,
    ) -> ResolvedFeature | None:
        del invariant_type
        if provenance.sample_sha256 not in self.positive_hashes_by_value.get(
            pattern.lower(), set()
        ):
            return None
        return ResolvedFeature(
            source_id=provenance.feature_id,
            sample_sha256=provenance.sample_sha256,
            feature_kind="string",
            normalized_value=pattern.lower(),
        )

    async def measure_feature(
        self,
        *,
        feature_kind: str,
        normalized_value: str,
        snapshot_sample_ids: Sequence[UUID],
    ) -> FeatureMeasurements:
        del feature_kind
        present_hashes = self.positive_hashes_by_value.get(normalized_value, set())
        support = sum(
            self.sample_hash_by_id.get(sample_id) in present_hashes
            for sample_id in snapshot_sample_ids
        )
        return FeatureMeasurements(benign_prevalence=0, positive_support=support)

    async def add_invariant(self, invariant: CandidateInvariant) -> CandidateInvariant:
        self.invariants_by_key[invariant.proposal_key] = invariant
        return invariant


class _MemoryRuleAssociations:
    def __init__(self, associations: Sequence[SampleAssociation]) -> None:
        self.associations = tuple(associations)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        return tuple(
            item for item in self.associations if item.investigation_id == investigation_id
        )


class _MemoryRuleCorpusInventory:
    def __init__(
        self, *, goodware: Sequence[RuleCorpusInventoryItem], holdout: Sequence[str]
    ) -> None:
        self.goodware = tuple(goodware)
        self.holdout = tuple(holdout)

    async def manual_rule_corpus_imports(
        self, investigation_id: UUID
    ) -> Sequence[RuleCorpusInventoryItem]:
        assert investigation_id == _INVESTIGATION_ID
        return self.goodware

    async def holdout_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        assert investigation_id == _INVESTIGATION_ID
        return self.holdout


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _case(payload: bytes, role: RuleTestCorpusRole) -> RuleTestCase:
    return RuleTestCase(sample_sha256=_digest(payload), role=role)


def _motif(value: str, *, sample_sha256: str, offset: int, provenance_id: str) -> RuleMotif:
    return RuleMotif(
        kind=RuleMotifKind.STRING,
        value=value,
        provenance_kind=RuleMotifProvenanceKind.BRIDGE,
        provenance_id=provenance_id,
        observation_refs=(uuid4(),),
        sample_sha256=sample_sha256,
        offsets=(offset,),
        rationale="Exact signature candidate linked to its L2 feature and P09 provenance.",
        positive_support=1,
    )


def _output_role(output: Mapping[str, Any], role: RuleTestCorpusRole) -> Mapping[str, Any]:
    report = output["rule"]["report"]
    return next(item for item in report["roles"] if item["role"] == role.value)


@pytest.mark.asyncio
async def test_real_bridge_uses_p09_and_rule_tools_and_replays_same_candidate() -> None:
    fragment_v1 = b"Qz9Vn5Hs"
    fragment_v2 = b"Ty7Kp3Wq"
    sample_a = b"prefix-a:" + fragment_v1 + b":tail"
    sample_b = b"a-much-longer-prefix:" + fragment_v1 + b":tail"
    sample_c = b"third-sample:" + fragment_v2 + b":tail"
    goodware = b"ordinary synthetic goodware"
    holdout = b"independent holdout:" + fragment_v2
    sample_hashes = tuple(_digest(item) for item in (sample_a, sample_b, sample_c))
    hash_a, hash_b, hash_c = sample_hashes
    goodware_hash, holdout_hash = _digest(goodware), _digest(holdout)
    samples = {_digest(item): item for item in (sample_a, sample_b, sample_c, goodware, holdout)}
    associations = _MemoryRuleAssociations(
        (
            SampleAssociation(
                investigation_id=_INVESTIGATION_ID,
                sample_sha256=hash_a,
                origin=SampleOrigin.SEED,
            ),
            SampleAssociation(
                investigation_id=_INVESTIGATION_ID,
                sample_sha256=hash_b,
                origin=SampleOrigin.SEED,
            ),
            SampleAssociation(
                investigation_id=_INVESTIGATION_ID,
                sample_sha256=hash_c,
                origin=SampleOrigin.RELATIONSHIP,
                qualification=SampleQualification.CANDIDATE,
            ),
        )
    )
    uow_factory = _FakeUowFactory(sample_hashes)
    sample_hash_by_id = {
        sample.id: uow_factory.uow.samples_by_blob[sample.blob_id]
        for sample in uow_factory.uow.sample_rows
    }
    invariant_repository = _MemoryInvariantRepository(
        sample_hash_by_id=sample_hash_by_id,
        positive_hashes_by_value={
            "qz9vn5hs": {hash_a, hash_b},
            "ty7kp3wq": {hash_c},
        },
    )
    uow_factory.uow.invariants = invariant_repository
    invariant_registry = InvariantRegistryService(uow_factory)  # type: ignore[arg-type]
    recorder = _FakeRecorder()
    candidate_repository = _MemoryCandidateRepository()
    corpus_bytes = _MemoryCorpus(samples)
    workbench = RuleWorkbench(
        corpus_bytes=corpus_bytes,  # type: ignore[arg-type]
        yara_engine=IsolatedYaraRunner(),
        detection_candidates=candidate_repository,  # type: ignore[arg-type]
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )
    inventory = _MemoryRuleCorpusInventory(
        goodware=(
            RuleCorpusInventoryItem(
                sample_sha256=goodware_hash,
                role=RuleTestCorpusRole.GOODWARE,
                provenance=RuleSampleProvenance.SYNTHETIC,
                size_bytes=len(goodware),
                source_description="Synthetic benign-like bytes for the local YARA test.",
            ),
        ),
        holdout=(holdout_hash,),
    )
    rule_registry = InMemoryToolRegistry()
    rule_tools = register_rule_tools(
        rule_registry,
        workbench=workbench,
        candidates=candidate_repository,  # type: ignore[arg-type]
        associations=associations,  # type: ignore[arg-type]
        corpus_bytes=corpus_bytes,  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        corpus_inventory=inventory,
    )
    initial_comparison = _comparison(
        sample_hashes=sample_hashes[:2],
        positive_hashes=sample_hashes[:2],
        qualified=True,
    )
    similarity = _FakeSimilarity(initial_comparison)
    bridge_registry = InMemoryToolRegistry()
    register_signature_bridge_tools(
        bridge_registry,
        service=similarity,  # type: ignore[arg-type]
        sample_associations=associations,  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        uow_factory=uow_factory,  # type: ignore[arg-type]
        invariant_registry=invariant_registry,
        rule_tools=rule_tools,
    )
    registration = bridge_registry.get("rule.propose_from_candidates", "1")
    assert registration is not None
    _definition, adapter = registration
    v1_arguments = {
        "sample_sha256s": list(sample_hashes[:2]),
        "feature_keys": [_FEATURE_KEY],
        "condition": "any",
    }

    v1_result = await adapter.execute(v1_arguments, _bridge_context())
    assert v1_result.status is ToolInvocationStatus.SUCCEEDED
    assert v1_result.output["rule"]["version"] == "v1"
    assert v1_result.output["rule"]["candidate_status"] == DetectionCandidateStatus.TESTED.value
    assert v1_result.output["rule"]["report"]["validation_status"] == "real_partial"
    goodware_case = _output_role(v1_result.output, RuleTestCorpusRole.GOODWARE)["test_cases"][0]
    assert goodware_case["provenance"] == RuleSampleProvenance.SYNTHETIC.value
    assert _output_role(v1_result.output, RuleTestCorpusRole.HOLDOUT)["non_matches"] == (
        holdout_hash,
    )
    assert _output_role(v1_result.output, RuleTestCorpusRole.HOLDOUT)["matches"] == ()
    assert _output_role(v1_result.output, RuleTestCorpusRole.GOODWARE)["non_matches"] == (
        goodware_hash,
    )
    assert (
        len(
            await candidate_repository.list_for_candidate(
                UUID(v1_result.output["rule"]["candidate_id"])
            )
        )
        == 1
    )
    stored_invariant = next(iter(invariant_repository.invariants_by_key.values()))
    assert stored_invariant.positive_sample_confirmed is False
    assert (
        next(
            item for item in associations.associations if item.sample_sha256 == hash_c
        ).qualification
        is SampleQualification.CANDIDATE
    )

    replay = await adapter.execute(v1_arguments, _bridge_context())
    assert replay.status is ToolInvocationStatus.SUCCEEDED
    assert replay.output["rule"]["replayed"] is True
    assert replay.output["rule"]["version_id"] == v1_result.output["rule"]["version_id"]
    assert (
        len(
            await candidate_repository.list_for_candidate(
                UUID(v1_result.output["rule"]["candidate_id"])
            )
        )
        == 1
    )

    similarity.comparison = _comparison(
        sample_hashes=sample_hashes,
        positive_hashes=(hash_c,),
        qualified=False,
        rejection_reasons=(FeatureRejectionReason.INSUFFICIENT_POSITIVE_SUPPORT,),
        pattern=fragment_v2.decode(),
    )
    v2_result = await adapter.execute(
        {
            "sample_sha256s": list(sample_hashes),
            "feature_keys": [_FEATURE_KEY],
            "condition": "any",
            "candidate_id": v1_result.output["rule"]["candidate_id"],
            "parent_version": 1,
        },
        _bridge_context(),
    )
    assert v2_result.status is ToolInvocationStatus.SUCCEEDED
    assert v2_result.output["rule"]["version"] == "v2"
    assert v2_result.output["rule"]["candidate_status"] == DetectionCandidateStatus.REJECTED.value
    assert _output_role(v2_result.output, RuleTestCorpusRole.HOLDOUT)["matches"] == (holdout_hash,)
    assert _output_role(v2_result.output, RuleTestCorpusRole.HOLDOUT)["non_matches"] == ()
    holdout_delta = next(
        item
        for item in v2_result.output["before_after"]["roles"]
        if item["role"] == RuleTestCorpusRole.HOLDOUT.value
    )
    assert holdout_delta["matches_gained"] == (holdout_hash,)
    assert v2_result.output["before_after"]["false_positives_added"] == (holdout_hash,)
    assert (
        next(
            item for item in associations.associations if item.sample_sha256 == hash_c
        ).qualification
        is SampleQualification.CANDIDATE
    )


@pytest.mark.asyncio
async def test_real_workbench_v1_v2_holdout_fp_and_trivial_motif_gate() -> None:
    fragment_v1 = b"Qz9Vn5Hs"
    fragment_v2 = b"Ty7Kp3Wq"
    sample_a = b"prefix-a:" + fragment_v1 + b":tail"
    sample_b = b"a-much-longer-prefix:" + fragment_v1 + b":tail"
    sample_c = b"third-sample:" + fragment_v2 + b":tail"
    goodware = b"ordinary synthetic goodware"
    holdout = b"synthetic holdout without a motif"
    samples = {_digest(item): item for item in (sample_a, sample_b, sample_c, goodware, holdout)}
    repository = _MemoryCandidateRepository()
    workbench = RuleWorkbench(
        corpus_bytes=_MemoryCorpus(samples),  # type: ignore[arg-type]
        yara_engine=IsolatedYaraRunner(),
        detection_candidates=repository,  # type: ignore[arg-type]
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )
    corpus = (
        _case(sample_a, RuleTestCorpusRole.CONFIRMED_POSITIVE),
        _case(sample_b, RuleTestCorpusRole.CONFIRMED_POSITIVE),
        _case(sample_c, RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE),
        _case(goodware, RuleTestCorpusRole.GOODWARE),
        _case(holdout, RuleTestCorpusRole.HOLDOUT),
    )
    v1_motif = _motif(
        "Qz9Vn5Hs",
        sample_sha256=_digest(sample_a),
        offset=9,
        provenance_id=_FEATURE_KEY,
    )
    v1 = await workbench.revise(
        None,
        new_motifs=(v1_motif,),
        new_condition=RuleConditionStrategy.ANY,
        investigation_id=_INVESTIGATION_ID,
        corpus=corpus,
        selection_bias="Two synthetic samples share one measured fragment.",
        coverage_statement="All positives, goodware, candidate, and holdout bytes were measured.",
    )
    v2_motif = _motif(
        "Ty7Kp3Wq",
        sample_sha256=_digest(sample_c),
        offset=13,
        provenance_id=hashlib.sha256(b"new feature").hexdigest(),
    )
    v2 = await workbench.revise(
        v1,
        new_motifs=(v1_motif, v2_motif),
        new_condition=RuleConditionStrategy.ANY,
        corpus=corpus,
        selection_bias="A new candidate sample escaped v1 and supplied a distinct feature.",
        coverage_statement="All test roles remained measurable in both versions.",
    )
    comparison = await workbench.compare(
        v1,
        v2,
        corpus,
        selection_bias="The same frozen synthetic corpus was retested.",
        coverage_statement="All test roles were measurable before and after.",
    )

    assert v1.version == 1 and v2.version == 2
    assert v2.parent_version_id == v1.id
    assert v1.status is not DetectionCandidateStatus.APPROVED
    assert v2.status is not DetectionCandidateStatus.APPROVED
    report_v1 = await workbench.compile_and_test(
        v1.source,
        corpus,
        selection_bias="measure v1 outcomes",
        coverage_statement="all synthetic corpus roles were measured",
    )
    report_v2 = await workbench.compile_and_test(
        v2.source,
        corpus,
        selection_bias="measure v2 outcomes",
        coverage_statement="all synthetic corpus roles were measured",
    )
    positive_v1 = next(
        role for role in report_v1.roles if role.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
    )
    assert set(positive_v1.matches) == {_digest(sample_a), _digest(sample_b)}
    candidate_v1 = next(
        role for role in report_v1.roles if role.role is RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE
    )
    assert candidate_v1.non_matches == (_digest(sample_c),)
    goodware_v1 = next(role for role in report_v1.roles if role.role is RuleTestCorpusRole.GOODWARE)
    assert goodware_v1.non_matches == (_digest(goodware),)
    out_of_cluster = next(
        role
        for role in comparison.roles
        if role.role is RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE
    )
    assert out_of_cluster.matches_gained == (_digest(sample_c),)
    assert comparison.false_positives_added == ()
    holdout_role = next(role for role in report_v1.roles if role.role is RuleTestCorpusRole.HOLDOUT)
    assert holdout_role.matches == ()
    assert holdout_role.non_matches == (_digest(holdout),)
    holdout_role_v2 = next(
        role for role in report_v2.roles if role.role is RuleTestCorpusRole.HOLDOUT
    )
    assert holdout_role_v2.matches == ()
    assert holdout_role_v2.non_matches == (_digest(holdout),)

    trivial = RuleMotif(
        kind=RuleMotifKind.STRING,
        value="administrator",
        provenance_kind=RuleMotifProvenanceKind.BRIDGE,
        provenance_id="trivial-feature",
        observation_refs=(uuid4(),),
    )
    assert any(
        issue.code is RuleMotifIssueCode.TRIVIAL
        for issue in workbench.validate_motifs((trivial,)).issues
    )

    fp_marker = "FalsePositiveMarker"
    fp_positive = b"positive:" + fp_marker.encode() + b":tail"
    fp_goodware = b"benign:" + fp_marker.encode() + b":tail"
    fp_samples = {_digest(fp_positive): fp_positive, _digest(fp_goodware): fp_goodware}
    fp_workbench = RuleWorkbench(
        corpus_bytes=_MemoryCorpus(fp_samples),  # type: ignore[arg-type]
        yara_engine=IsolatedYaraRunner(),
        detection_candidates=_MemoryCandidateRepository(),  # type: ignore[arg-type]
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )
    fp_version = await fp_workbench.revise(
        None,
        new_motifs=(
            RuleMotif(
                kind=RuleMotifKind.STRING,
                value=fp_marker,
                provenance_kind=RuleMotifProvenanceKind.BRIDGE,
                provenance_id="fp-feature",
                observation_refs=(uuid4(),),
            ),
        ),
        new_condition=RuleConditionStrategy.ANY,
        investigation_id=uuid4(),
        corpus=(
            _case(fp_positive, RuleTestCorpusRole.CONFIRMED_POSITIVE),
            _case(fp_goodware, RuleTestCorpusRole.GOODWARE),
        ),
        selection_bias="Synthetic false positive regression case.",
        coverage_statement="One positive and one goodware sample were measured.",
    )
    assert fp_version.status is DetectionCandidateStatus.REJECTED

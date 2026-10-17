from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.application.investigation_persistence import (
    InMemoryDetectionCandidateRepository,
    InMemoryEvidenceObservationRepository,
    InMemorySampleAssociationRepository,
)
from cti_app.application.investigation_rule_tools import (
    JSON_SCHEMA_2020_12,
    RULE_COMPARE_SCHEMA,
    RULE_GET_SCHEMA,
    RULE_PROPOSE_SCHEMA,
    RULE_REVISE_SCHEMA,
    RULE_TEST_SCHEMA,
    RULE_WORKBENCH_CAPABILITY,
    QuarantineRuleCorpusBytes,
    _version_summary,
    register_rule_tools,
    rule_tool_capabilities,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
    validate_json_schema,
)
from cti_app.application.rule_workbench import (
    RuleCorpusBytesUnavailable,
    RuleWorkbench,
    YaraSampleInput,
)
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.goodware import NonDiscriminantPatternRegistry
from cti_app.domain.investigation import (
    BudgetDimension,
    DetectionCandidateStatus,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationEnvelope,
    QualificationActorKind,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.rule_workbench import (
    RuleMotifKind,
    RuleMotifProvenanceKind,
    YaraEngineResult,
    YaraExecutionStatus,
)
from tests.test_investigation_service import (
    _decision,
    _finding_id,
    _Fixture,
    _operation_and_observation_id,
    _opinion_delta,
    _proposal,
    _tool_call,
)

_EVIDENCE_NAMESPACE = UUID("47bfec4f-9070-42d0-aef5-0eb84c8b57cd")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _MemoryCorpusBytes:
    def __init__(self, samples: Mapping[str, bytes]) -> None:
        self.samples = dict(samples)

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        try:
            return self.samples[sample_sha256]
        except KeyError as exc:
            raise RuleCorpusBytesUnavailable("fixture bytes unavailable") from exc

    async def goodware_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        del investigation_id
        return tuple(sorted(self.goodware)) if hasattr(self, "goodware") else ()

    async def holdout_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        del investigation_id
        return tuple(sorted(self.holdout)) if hasattr(self, "holdout") else ()


class _DeterministicYaraEngine:
    def __init__(self) -> None:
        self.compile_count = 0
        self.match_count = 0

    def compile(self, source: str) -> YaraEngineResult:
        self.compile_count += 1
        if "condition:" not in source:
            return YaraEngineResult(
                status=YaraExecutionStatus.COMPILE_ERROR,
                engine_version="fixture-yara-1",
                message="invalid synthetic source",
                line=1,
            )
        return YaraEngineResult(status=YaraExecutionStatus.OK, engine_version="fixture-yara-1")

    def match(self, source: str, samples: Sequence[YaraSampleInput]) -> YaraEngineResult:
        self.match_count += 1
        strings_section = source.split("strings:", maxsplit=1)[1].split("condition:", maxsplit=1)[0]
        patterns = [
            ast.literal_eval(match.group(1)).encode("utf-8")
            for match in re.finditer(r'^\s*\$m\d+\s*=\s*("(?:\\.|[^"\\])*")', strings_section, re.M)
        ]
        matched = tuple(
            sample.sample_sha256
            for sample in samples
            if any(pattern in sample.data for pattern in patterns)
        )
        return YaraEngineResult(
            status=YaraExecutionStatus.OK,
            engine_version="fixture-yara-1",
            matches_sha256=matched,
        )


class _UnavailableYaraEngine:
    def compile(self, source: str) -> YaraEngineResult:
        del source
        return YaraEngineResult(
            status=YaraExecutionStatus.ENGINE_UNAVAILABLE,
            engine_version="unavailable",
            message="simulated missing yara-python",
        )

    def match(self, source: str, samples: Sequence[YaraSampleInput]) -> YaraEngineResult:
        del source, samples
        raise AssertionError("match must not run after an unavailable compilation")


class _MemoryEvidence:
    def __init__(self, repository: InMemoryEvidenceObservationRepository | None = None) -> None:
        self.repository = repository or InMemoryEvidenceObservationRepository()
        self.payloads: dict[UUID, dict[str, Any]] = {}

    async def record_json(
        self,
        *,
        investigation_id: UUID,
        kind: EvidenceKind,
        payload: Mapping[str, Any],
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...] = (),
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        blob_id = uuid5(_EVIDENCE_NAMESPACE, f"blob:{digest}")
        observation_id = uuid5(_EVIDENCE_NAMESPACE, idempotency_key) if idempotency_key else uuid4()
        observation = EvidenceObservation(
            id=observation_id,
            investigation_id=investigation_id,
            kind=kind,
            blob_id=blob_id,
            sha256=digest,
            source=source,
            source_version=source_version,
            observed_at=observed_at or datetime.now(UTC),
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
            invocation_ref=invocation_ref,
        )
        existing = await self.repository.get(observation.id)
        if existing is not None:
            assert existing == observation
        else:
            observation = await self.repository.append_if_absent(observation)
        self.payloads[blob_id] = json.loads(serialized)
        return observation

    async def record_existing_blob(self, **_kwargs: Any) -> EvidenceObservation:
        raise AssertionError("rule tools only record bounded JSON reports")

    async def record_negative(self, **_kwargs: Any) -> EvidenceObservation:
        raise AssertionError("rule tools do not record negative observations")

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.payloads[blob_id]


class _MemoryInventory(_MemoryCorpusBytes):
    def __init__(
        self, samples: Mapping[str, bytes], *, goodware=(), holdout=(), manual_imports=()
    ) -> None:
        super().__init__(samples)
        self.goodware = tuple(goodware)
        self.holdout = tuple(holdout)
        self._manual_imports = tuple(manual_imports)

    async def manual_rule_corpus_imports(self, investigation_id: UUID):
        from cti_app.domain.rule_workbench import (
            RuleCorpusInventoryItem,
            RuleSampleProvenance,
            RuleTestCorpusRole,
        )

        del investigation_id
        goodware = tuple(
            RuleCorpusInventoryItem(
                sample_sha256=sample_sha256,
                role=RuleTestCorpusRole.GOODWARE,
                provenance=RuleSampleProvenance.REAL,
                size_bytes=len(self.samples.get(sample_sha256, b"")),
                source_description="Operator-imported benign test fixture.",
            )
            for sample_sha256 in self.goodware
        )
        return (*goodware, *self._manual_imports)


@dataclass
class _ToolHarness:
    investigation_id: UUID
    candidates: InMemoryDetectionCandidateRepository
    associations: InMemorySampleAssociationRepository
    bytes_port: _MemoryCorpusBytes
    evidence: _MemoryEvidence
    engine: Any
    registry: InMemoryToolRegistry
    dispatcher: RegistryToolDispatcher
    rule_tools: Any
    context: ToolExecutionContext


def _harness(
    *,
    samples: Mapping[str, bytes],
    goodware: Sequence[str] = (),
    holdout: Sequence[str] = (),
    manual_imports: Sequence[Any] = (),
    article_claims: Any | None = None,
    engine: Any | None = None,
    evidence_repository: InMemoryEvidenceObservationRepository | None = None,
) -> _ToolHarness:
    investigation_id = uuid4()
    candidates = InMemoryDetectionCandidateRepository()
    association_repository = InMemorySampleAssociationRepository()
    bytes_port = _MemoryInventory(
        samples, goodware=goodware, holdout=holdout, manual_imports=manual_imports
    )
    evidence = _MemoryEvidence(evidence_repository)
    selected_engine = engine or _DeterministicYaraEngine()
    workbench = RuleWorkbench(
        corpus_bytes=bytes_port,
        yara_engine=selected_engine,
        detection_candidates=candidates,
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )
    capabilities = rule_tool_capabilities(
        workbench=workbench,
        candidates=candidates,
        associations=association_repository,
        corpus_bytes=bytes_port,
        recorder=evidence,
        evidence_observations=evidence.repository,
        evidence_payloads=evidence,
    )
    registry = InMemoryToolRegistry()
    rule_tools = register_rule_tools(
        registry,
        workbench=workbench,
        candidates=candidates,
        associations=association_repository,
        corpus_bytes=bytes_port,
        recorder=evidence,
        evidence_observations=evidence.repository,
        evidence_payloads=evidence,
        article_claims=article_claims,
    )
    budget = InvestigationBudget(
        limits={BudgetDimension.TOOL_SECONDS: 600},
    )
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=(),
        budget=budget,
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-test",
    )
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=budget,
        idempotency_key=f"rule-test:{uuid4()}",
        available_capabilities=capabilities,
    )
    harness = _ToolHarness(
        investigation_id=investigation_id,
        candidates=candidates,
        associations=association_repository,
        bytes_port=bytes_port,
        evidence=evidence,
        engine=selected_engine,
        registry=registry,
        dispatcher=RegistryToolDispatcher(registry),
        rule_tools=rule_tools,
        context=context,
    )
    return harness


def _motif(
    value: str, observation_ref: UUID, *, provenance_id: str | None = None
) -> dict[str, Any]:
    return {
        "kind": RuleMotifKind.STRING.value,
        "value": value,
        "provenance_kind": RuleMotifProvenanceKind.P09_INVARIANT.value,
        "provenance_id": provenance_id or str(uuid4()),
        "observation_refs": [str(observation_ref)],
    }


async def _source_observation(harness: _ToolHarness, motif: str) -> UUID:
    observation = await harness.evidence.record_json(
        investigation_id=harness.investigation_id,
        kind=EvidenceKind.BINARY_STRING,
        payload={"schema": "fixture-feature.v1", "sample_sha256": "1" * 64},
        source="synthetic fixture",
        source_version="1",
        object_type="binary_string_feature",
        object_id=hashlib.sha256(motif.encode()).hexdigest(),
        limitations=("Fixture content is synthetic and intentionally inert.",),
        idempotency_key=f"feature:{uuid4()}",
    )
    return observation.id


async def _call(
    harness: _ToolHarness,
    name: str,
    arguments: Mapping[str, Any],
    *,
    operation_key: str | None = None,
):
    return await harness.dispatcher.dispatch(
        harness.context,
        tool_name=name,
        schema_version="1",
        arguments=arguments,
        purpose=f"test {name}",
        operation_key=operation_key or f"op:{uuid4()}",
    )


async def _associate(
    harness: _ToolHarness,
    sample_sha256: str,
    *,
    origin: SampleOrigin = SampleOrigin.SEED,
    qualification: SampleQualification = SampleQualification.CANDIDATE,
) -> SampleAssociation:
    association = SampleAssociation(
        investigation_id=harness.investigation_id,
        sample_sha256=sample_sha256,
        origin=origin,
        qualification=qualification,
    )
    return await harness.associations.append_version(association, expected_version=0)


@pytest.mark.asyncio
async def test_tool_schemas_capabilities_and_approval_are_closed() -> None:
    harness = _harness(samples={})
    capabilities = harness.context.available_capabilities
    assert capabilities == frozenset(
        {
            RULE_WORKBENCH_CAPABILITY,
            "rule.propose",
            "rule.test",
            "rule.revise",
            "rule.compare",
            "rule.get",
        }
    )
    for name in ("rule.propose", "rule.test", "rule.revise", "rule.compare", "rule.get"):
        registered = harness.registry.get(name, "1")
        assert registered is not None
        definition, _adapter = registered
        assert definition.input_schema["$schema"] == JSON_SCHEMA_2020_12
        assert definition.input_schema["additionalProperties"] is False
    assert harness.registry.get("rule.approve", "1") is None
    test_candidate_description = RULE_TEST_SCHEMA["properties"]["candidate_id"]["description"]
    assert "candidate_id returned by rule.propose" in test_candidate_description
    assert "rule_deltas" in RULE_REVISE_SCHEMA["properties"]["candidate_id"]["description"]
    assert RULE_PROPOSE_SCHEMA["properties"]["meta"]["default"] == {}
    assert "Optional JSON object" in RULE_PROPOSE_SCHEMA["properties"]["meta"]["description"]
    assert "ARG meta:" in RULE_REVISE_SCHEMA["properties"]["meta"]["description"]
    revise_registration = harness.registry.get("rule.revise", "1")
    assert revise_registration is not None
    assert "status" not in revise_registration[0].input_schema["properties"]

    invalid = await _call(
        harness,
        "rule.propose",
        {"condition": "any", "motifs": []},
    )
    assert invalid.status is ToolInvocationStatus.FAILED
    assert harness.engine.compile_count == 0


def test_optional_rule_tool_arguments_accept_empty_wire_values() -> None:
    candidate_id = str(uuid4())
    validate_json_schema(
        {
            "candidate_id": "",
            "condition": "all",
            "motifs": "",
            "feature_refs": [],
            "meta": "",
        },
        RULE_PROPOSE_SCHEMA,
    )
    validate_json_schema(
        {
            "candidate_id": candidate_id,
            "parent_version": 1,
            "condition": "",
            "motifs": "",
            "meta": {},
        },
        RULE_REVISE_SCHEMA,
    )
    validate_json_schema(
        {"candidate_id": candidate_id, "version": ""},
        RULE_TEST_SCHEMA,
    )
    validate_json_schema(
        {"candidate_id": candidate_id, "version": "", "include_history": ""},
        RULE_GET_SCHEMA,
    )
    validate_json_schema(
        {"candidate_id": candidate_id, "version_a": 1, "version_b": 2},
        RULE_COMPARE_SCHEMA,
    )


@pytest.mark.asyncio
async def test_non_json_meta_returns_actionable_example_without_creating_candidate() -> None:
    harness = _harness(samples={})
    observation_ref = await _source_observation(harness, "MetaExampleMarker")
    candidate_id = uuid4()

    result = await _call(
        harness,
        "rule.propose",
        {
            "candidate_id": str(candidate_id),
            "condition": "all",
            "motifs": [_motif("MetaExampleMarker", observation_ref)],
            "meta": "not JSON",
        },
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert 'ARG meta: {"description": "..."}' in result.message
    assert await harness.candidates.list_for_candidate(candidate_id) == ()


@pytest.mark.asyncio
async def test_json_encoded_meta_object_is_accepted() -> None:
    harness = _harness(samples={})
    observation_ref = await _source_observation(harness, "EncodedMetaMarker")

    result = await _call(
        harness,
        "rule.propose",
        {
            "condition": "all",
            "motifs": [_motif("EncodedMetaMarker", observation_ref)],
            "meta": '{"description": "synthetic metadata"}',
        },
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.error_code is None
    assert result.output["candidate_id"]


def test_rule_version_summary_without_report_is_explicitly_untested() -> None:
    version = SimpleNamespace(
        id=uuid4(),
        candidate_id=uuid4(),
        investigation_id=uuid4(),
        version=1,
        version_label="v1",
        parent_version_id=None,
        source="rule candidate { condition: true }",
        source_sha256="a" * 64,
        status=DetectionCandidateStatus.CANDIDATE,
        test_refs=(),
        motif_provenance=(),
    )

    summary = _version_summary(version, ())

    assert summary["validation_status"] == "untested"
    assert summary["latest_test_report"] is None


@pytest.mark.asyncio
async def test_rule_get_marks_version_without_report_untested() -> None:
    from cti_app.domain.investigation import DetectionCandidateVersion

    harness = _harness(samples={})
    source = "rule untested { condition: true }"
    candidate_id = uuid4()
    version = DetectionCandidateVersion(
        investigation_id=harness.investigation_id,
        candidate_id=candidate_id,
        source=source,
        source_sha256=_sha256(source.encode()),
        motif_provenance=(),
    )
    await harness.candidates.append_version(version, expected_previous_id=None)

    result = await _call(harness, "rule.get", {"candidate_id": str(candidate_id)})

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["versions"][0]["validation_status"] == "untested"
    assert result.output["versions"][0]["latest_test_report"] is None


@pytest.mark.asyncio
async def test_invalid_trivial_motif_is_rejected_before_compile() -> None:
    data = b"safe synthetic sample"
    sample_sha256 = _sha256(data)
    harness = _harness(samples={sample_sha256: data})
    await _associate(harness, sample_sha256)
    observation_ref = await _source_observation(harness, "the")

    result = await _call(
        harness,
        "rule.propose",
        {
            "condition": "any",
            "motifs": [_motif("the", observation_ref)],
        },
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.output["reason"] == "motif_validation_failed"
    assert harness.engine.compile_count == 0
    assert await harness.candidates.list_for_candidate(uuid4()) == ()


@pytest.mark.asyncio
async def test_missing_engine_and_unreadable_bytes_are_local_unavailable_results() -> None:
    data = b"synthetic sample with MarkerForTest"
    sha256 = _sha256(data)
    unavailable_engine = _harness(
        samples={sha256: data},
        engine=_UnavailableYaraEngine(),
    )
    await _associate(unavailable_engine, sha256)
    source_ref = await _source_observation(unavailable_engine, "MarkerForTest")
    unavailable = await _call(
        unavailable_engine,
        "rule.propose",
        {"condition": "any", "motifs": [_motif("MarkerForTest", source_ref)]},
    )
    assert unavailable.status is ToolInvocationStatus.UNAVAILABLE
    assert unavailable.output["execution_status"] == "engine_unavailable"
    assert (
        await unavailable_engine.candidates.list_for_candidate(
            UUID(unavailable.output["candidate_id"])
        )
        == ()
    )

    missing_bytes = _harness(samples={})
    await _associate(missing_bytes, sha256)
    source_ref = await _source_observation(missing_bytes, "MarkerForTest")
    non_measurable = await _call(
        missing_bytes,
        "rule.propose",
        {"condition": "any", "motifs": [_motif("MarkerForTest", source_ref)]},
    )
    assert non_measurable.status is ToolInvocationStatus.UNAVAILABLE
    report_ref = UUID(non_measurable.output["observation_refs"][0])
    observation = await missing_bytes.evidence.repository.get(report_ref)
    assert observation is not None
    assert observation.kind is EvidenceKind.YARA_MATCH
    report = missing_bytes.evidence.payloads[observation.blob_id]
    positive = next(role for role in report["roles"] if role["role"] == "confirmed_positive")
    assert positive["non_measurable"] == [sha256]
    assert positive["non_matches"] == []
    assert "data" not in json.dumps(report).lower()
    assert "MarkerForTest" not in json.dumps(report)


@pytest.mark.asyncio
async def test_v1_v2_retest_and_comparison_record_exact_corpus_observations() -> None:
    first_bytes = b"safe fixture with AlphaFamilyMarker"
    second_bytes = b"safe fixture with BetaVariantMarker"
    goodware_bytes = b"ordinary synthetic goodware, unrelated"
    holdout_bytes = b"independent synthetic holdout, unrelated"
    first_sha, second_sha = _sha256(first_bytes), _sha256(second_bytes)
    goodware_sha, holdout_sha = _sha256(goodware_bytes), _sha256(holdout_bytes)
    harness = _harness(
        samples={
            first_sha: first_bytes,
            second_sha: second_bytes,
            goodware_sha: goodware_bytes,
            holdout_sha: holdout_bytes,
        },
        goodware=(goodware_sha,),
        holdout=(holdout_sha,),
    )
    await _associate(harness, first_sha, origin=SampleOrigin.SEED)
    marker_one_ref = await _source_observation(harness, "AlphaFamilyMarker")
    marker_two_ref = await _source_observation(harness, "BetaVariantMarker")
    candidate_id = uuid4()

    v1_result = await _call(
        harness,
        "rule.propose",
        {
            "candidate_id": str(candidate_id),
            "condition": "any",
            "motifs": [_motif("AlphaFamilyMarker", marker_one_ref)],
        },
    )
    assert v1_result.status is ToolInvocationStatus.SUCCEEDED
    assert v1_result.output["version"] == "v1"
    assert v1_result.output["candidate_status"] == DetectionCandidateStatus.EXPERIMENTAL.value
    v1 = (await harness.candidates.list_for_candidate(candidate_id))[0]
    assert v1.status is DetectionCandidateStatus.EXPERIMENTAL

    added = await _associate(
        harness,
        second_sha,
        origin=SampleOrigin.MANUAL,
        qualification=SampleQualification.CANDIDATE,
    )
    confirmed = added.change_qualification(
        SampleQualification.CONFIRMED,
        actor_kind=QualificationActorKind.HUMAN,
        actor_id="analyst-test",
        reason="Synthetic fixture is part of the confirmed test cluster.",
    )
    await harness.associations.append_version(confirmed, expected_version=added.version)

    v2_result = await _call(
        harness,
        "rule.revise",
        {
            "candidate_id": str(candidate_id),
            "parent_version": 1,
            "condition": "any",
            "motifs": [
                _motif("AlphaFamilyMarker", marker_one_ref),
                _motif("BetaVariantMarker", marker_two_ref),
            ],
        },
    )
    assert v2_result.status is ToolInvocationStatus.SUCCEEDED
    assert v2_result.output["version"] == "v2"
    assert v2_result.output["source_sha256"] != v1.source_sha256
    v2 = (await harness.candidates.list_for_candidate(candidate_id))[-1]
    assert v2.parent_version_id == v1.id
    assert v2.status is DetectionCandidateStatus.TESTED

    compared = await _call(
        harness,
        "rule.compare",
        {"candidate_id": str(candidate_id), "version_a": 1, "version_b": 2},
    )
    assert compared.status is ToolInvocationStatus.SUCCEEDED
    positive_delta = next(
        role for role in compared.output["roles"] if role["role"] == "confirmed_positive"
    )
    assert positive_delta["matches_gained"] == (second_sha,)
    assert compared.output["finding_candidate"]["kind"] == "detection"
    comparison_observation = await harness.evidence.repository.get(compared.observation_refs[0])
    assert comparison_observation is not None
    assert comparison_observation.kind is EvidenceKind.YARA_MATCH

    v1_observation = await harness.evidence.repository.get(v1_result.observation_refs[0])
    assert v1_observation is not None and v1_observation.kind is EvidenceKind.YARA_MATCH
    v1_payload = harness.evidence.payloads[v1_observation.blob_id]
    role_hashes = v1_payload["corpus_sha256s_by_role"]
    assert role_hashes["confirmed_positive"] == [first_sha]
    assert role_hashes["goodware"] == [goodware_sha]
    assert role_hashes["holdout"] == [holdout_sha]
    for payload in harness.evidence.payloads.values():
        serialized = json.dumps(payload, sort_keys=True)
        assert first_bytes.decode() not in serialized
        assert second_bytes.decode() not in serialized
        assert "AlphaFamilyMarker" not in serialized
        assert "BetaVariantMarker" not in serialized
        assert 'source": "rule' not in serialized

    history = await _call(
        harness,
        "rule.get",
        {"candidate_id": str(candidate_id), "include_history": True},
    )
    assert history.status is ToolInvocationStatus.SUCCEEDED
    assert [item["version"] for item in history.output["versions"]] == ["v1", "v2"]
    assert history.output["comparisons"]
    serialized_history = json.dumps(
        history.output,
        default=lambda value: dict(value) if isinstance(value, Mapping) else str(value),
    )
    assert '"source":' not in serialized_history
    assert "rule " not in serialized_history.lower()


@pytest.mark.asyncio
async def test_dispatcher_replay_returns_cached_rule_test_without_recompilation() -> None:
    data = b"synthetic sample with StableToolMarker"
    sha256 = _sha256(data)
    harness = _harness(samples={sha256: data}, goodware=(), holdout=())
    await _associate(harness, sha256)
    ref = await _source_observation(harness, "StableToolMarker")
    candidate = uuid4()
    proposed = await _call(
        harness,
        "rule.propose",
        {
            "candidate_id": str(candidate),
            "condition": "any",
            "motifs": [_motif("StableToolMarker", ref)],
        },
    )
    operation_key = "stable-rule-test-operation"
    args = {"candidate_id": str(candidate), "version": 1}
    before = harness.engine.compile_count
    first = await _call(harness, "rule.test", args, operation_key=operation_key)
    second = await _call(harness, "rule.test", args, operation_key=operation_key)
    assert first.status is ToolInvocationStatus.PARTIAL
    assert second == first
    assert harness.engine.compile_count == before + 1
    # With no operator-supplied benign bytes, the run records a partial
    # evaluation instead of implying negative-corpus coverage.
    assert proposed.status is ToolInvocationStatus.PARTIAL


@pytest.mark.asyncio
async def test_goodware_match_rejects_rule_without_an_approval_path() -> None:
    positive_bytes = b"synthetic cluster payload with SharedNoisyMarker"
    goodware_bytes = b"ordinary synthetic application with SharedNoisyMarker"
    positive_sha, goodware_sha = _sha256(positive_bytes), _sha256(goodware_bytes)
    harness = _harness(
        samples={positive_sha: positive_bytes, goodware_sha: goodware_bytes},
        goodware=(goodware_sha,),
    )
    await _associate(harness, positive_sha)
    ref = await _source_observation(harness, "SharedNoisyMarker")
    candidate = uuid4()

    result = await _call(
        harness,
        "rule.propose",
        {
            "candidate_id": str(candidate),
            "condition": "any",
            "motifs": [_motif("SharedNoisyMarker", ref)],
        },
    )

    version = (await harness.candidates.list_for_candidate(candidate))[0]
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert version.status is DetectionCandidateStatus.REJECTED
    assert harness.registry.get("rule.approve", "1") is None
    assert not any(
        item.status is DetectionCandidateStatus.APPROVED
        for item in await harness.candidates.list_for_candidate(candidate)
    )


@pytest.mark.asyncio
async def test_noisy_rule_does_not_block_real_loop_opinion_draft() -> None:
    positive_bytes = b"synthetic chain payload with SharedChainMarker"
    goodware_bytes = b"ordinary synthetic application with SharedChainMarker"
    positive_sha, goodware_sha = _sha256(positive_bytes), _sha256(goodware_bytes)
    fixture = _Fixture(responses=[], tools=(("chain.inspect", "local"),))
    state = await fixture.start()
    association_repository = InMemorySampleAssociationRepository()
    await association_repository.append_version(
        SampleAssociation(
            investigation_id=state.investigation_id,
            sample_sha256=positive_sha,
            origin=SampleOrigin.SEED,
        ),
        expected_version=0,
    )
    candidates = InMemoryDetectionCandidateRepository()
    bytes_port = _MemoryInventory(
        {positive_sha: positive_bytes, goodware_sha: goodware_bytes},
        goodware=(goodware_sha,),
    )
    evidence = _MemoryEvidence(fixture.observations)
    engine = _DeterministicYaraEngine()
    workbench = RuleWorkbench(
        corpus_bytes=bytes_port,
        yara_engine=engine,
        detection_candidates=candidates,
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )
    rule_capabilities = rule_tool_capabilities(
        workbench=workbench,
        candidates=candidates,
        associations=association_repository,
        corpus_bytes=bytes_port,
        recorder=evidence,
        evidence_observations=fixture.observations,
        evidence_payloads=evidence,
    )
    register_rule_tools(
        fixture.registry,
        workbench=workbench,
        candidates=candidates,
        associations=association_repository,
        corpus_bytes=bytes_port,
        recorder=evidence,
        evidence_observations=fixture.observations,
        evidence_payloads=evidence,
    )
    fixture.available_capabilities = fixture.available_capabilities | rule_capabilities
    fixture.service._available_capabilities = fixture.available_capabilities

    candidate_id = uuid4()
    local_context = ToolExecutionContext(
        investigation_id=state.investigation_id,
        envelope=fixture.envelope,
        budget=fixture.envelope.budget,
        idempotency_key="feature-evidence",
        available_capabilities=fixture.available_capabilities,
    )
    marker_ref = await _source_observation(
        _ToolHarness(
            investigation_id=state.investigation_id,
            candidates=candidates,
            associations=association_repository,
            bytes_port=bytes_port,
            evidence=evidence,
            engine=engine,
            registry=fixture.registry,
            dispatcher=RegistryToolDispatcher(fixture.registry),
            rule_tools=None,
            context=local_context,
        ),
        "SharedChainMarker",
    )
    rule_call = {
        "tool_name": "rule.propose",
        "schema_version": "1",
        "arguments": {
            "candidate_id": str(candidate_id),
            "condition": "any",
            "motifs": [_motif("SharedChainMarker", marker_ref)],
        },
        "purpose": "check a local candidate pattern",
    }
    chain_call = _tool_call("chain.inspect", "loader downloads payload", "clarify infection chain")
    _chain_operation, chain_observation_id = _operation_and_observation_id(
        state.investigation_id, chain_call
    )
    chain_finding = _proposal(
        "infection_chain",
        "supported",
        "The loader chain observation links the initial stage to payload retrieval.",
        chain_observation_id,
        subject_ref="loader downloads payload",
    )
    fixture.tools["chain.inspect"].observation_object_type = "infection_chain"
    fixture.tools["chain.inspect"].observation_object_id = "loader downloads payload"
    finding_id = _finding_id(state.investigation_id, chain_finding)
    fixture.bridge_transport = ScriptedTurnTransport(
        [
            _decision(
                goal="test the candidate while documenting the chain",
                next_action="continue",
                tools=(rule_call, chain_call),
            ),
            _decision(
                goal="finish the infection-chain finding",
                next_action="finish",
                findings=(chain_finding,),
                opinion=_opinion_delta(
                    finding_id,
                    chain_observation_id,
                    text=(
                        "The investigation clarifies payload retrieval; the noisy YARA "
                        "candidate was rejected."
                    ),
                ),
            ),
        ]
    )
    fixture.bridge = BridgeInvestigator(fixture.bridge_transport)
    fixture.service._bridge = fixture.bridge

    result = await fixture.service.run(state.investigation_id)

    rejected = (await candidates.list_for_candidate(candidate_id))[0]
    assert rejected.status is DetectionCandidateStatus.REJECTED
    assert result.stop_reason.value == "agent_finished"
    opinions = list(fixture.opinions._items.values())
    assert len(opinions) == 1
    assert opinions[0].status.value == "draft"
    assert opinions[0].blocks
    assert opinions[0].blocks[0].finding_refs == (finding_id,)
    assert await fixture.observations.get(chain_observation_id) is not None


@pytest.mark.asyncio
async def test_investigation_chain_can_finish_without_any_rule_tool() -> None:
    fixture = _Fixture(responses=[], tools=(("chain.inspect", "local"),))
    state = await fixture.start()
    chain_call = _tool_call(
        "chain.inspect", "loader retrieves payload", "trace the infection chain"
    )
    _operation, observation_id = _operation_and_observation_id(state.investigation_id, chain_call)
    proposal = _proposal(
        "infection_chain",
        "qualified",
        "The loader observation identifies payload retrieval as the next stage.",
        observation_id,
    )
    finding_id = _finding_id(state.investigation_id, proposal)
    opinion = _opinion_delta(finding_id, observation_id)
    opinion["blocks"][0]["qualification"] = "inference"
    opinion["blocks"][0]["certainty"] = 0.6
    fixture.bridge_transport = ScriptedTurnTransport(
        [
            _decision(
                goal="clarify the infection chain",
                next_action="finish",
                tools=(chain_call,),
                findings=(proposal,),
                opinion=opinion,
            )
        ]
    )
    fixture.bridge = BridgeInvestigator(fixture.bridge_transport)
    fixture.service._bridge = fixture.bridge

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason.value == "agent_finished"
    assert fixture.registry.get("rule.propose", "1") is None
    available_names = {
        definition.name
        for definition in fixture.registry.list_available(
            ToolExecutionContext(
                investigation_id=state.investigation_id,
                envelope=fixture.envelope,
                budget=fixture.envelope.budget,
                idempotency_key="without-rule",
                available_capabilities=fixture.available_capabilities,
            )
        )
    }
    assert "rule.propose" not in available_names
    assert fixture.opinions._items


@pytest.mark.asyncio
async def test_quarantine_corpus_bytes_are_hash_checked_and_read_bounded() -> None:
    data = b"synthetic quarantined sample"
    sha256 = _sha256(data)
    descriptor = BlobDescriptor(
        logical_bucket="samples-quarantine",
        sha256=sha256,
        size=len(data),
        mime_type="application/octet-stream",
    )
    record = SimpleNamespace(descriptor=descriptor)

    class BlobRepository:
        async def get_by_address(self, bucket: str, digest: str):
            return record if bucket == "samples-quarantine" and digest == sha256 else None

    class UnitOfWork:
        blobs = BlobRepository()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Store:
        def __init__(self):
            self.max_bytes = None

        async def read(self, observed_descriptor, *, max_bytes):
            assert observed_descriptor == descriptor
            self.max_bytes = max_bytes
            return data

    class UowFactory:
        def __call__(self):
            return UnitOfWork()

    store = Store()
    reader = QuarantineRuleCorpusBytes(UowFactory(), store, maximum_sample_bytes=1024)
    assert await reader.get_bytes_for_sample(sha256) == data
    assert store.max_bytes == 1024

    oversized_descriptor = BlobDescriptor(
        logical_bucket="samples-quarantine",
        sha256=sha256,
        size=2048,
        mime_type="application/octet-stream",
    )
    record.descriptor = oversized_descriptor
    with pytest.raises(RuleCorpusBytesUnavailable):
        await reader.get_bytes_for_sample(sha256)


@pytest.mark.asyncio
async def test_goodware_feature_baseline_is_not_reported_as_tested_goodware_bytes() -> None:
    from cti_app.application.investigation_rule_tools import _RuleTools
    from cti_app.domain.rule_workbench import RuleTestCorpusRole

    baseline_hash = "a" * 64
    investigation_id = uuid4()

    class Associations:
        async def list_for_investigation(self, _investigation_id):
            return ()

    class BaselineOnlyInventory:
        async def goodware_sample_sha256s(self, _investigation_id):
            return (baseline_hash,)

        async def holdout_sample_sha256s(self, _investigation_id):
            return ()

    tools = _RuleTools(
        workbench=None,
        candidates=None,
        associations=Associations(),
        corpus_bytes=None,
        recorder=None,
        evidence_observations=None,
        evidence_payloads=None,
        corpus_inventory=BaselineOnlyInventory(),
        feature_provider=None,
        max_tool_seconds=30,
    )

    corpus = await tools._build_corpus(investigation_id)

    assert not any(case.role is RuleTestCorpusRole.GOODWARE for case in corpus.cases)
    assert "no goodware binaries" in corpus.coverage_statement.casefold()


@pytest.mark.asyncio
async def test_synthetic_positive_and_real_goodware_report_provenance() -> None:
    from cti_app.domain.rule_workbench import (
        RuleCorpusInventoryItem,
        RuleSampleProvenance,
        RuleTestCorpusRole,
    )

    positive = (
        b"<configuration><runtime><appDomainManagerAssembly>"
        b"SyntheticManager, Version=1</appDomainManagerAssembly></runtime></configuration>"
    )
    goodware = (
        b"<configuration><startup><supportedRuntime version='v4.0'/></startup></configuration>"
    )
    positive_sha, goodware_sha = _sha256(positive), _sha256(goodware)
    imported_positive = RuleCorpusInventoryItem(
        sample_sha256=positive_sha,
        role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
        provenance=RuleSampleProvenance.SYNTHETIC,
        size_bytes=len(positive),
        source_description="Synthetic configuration from the cited article fragment.",
    )
    harness = _harness(
        samples={positive_sha: positive, goodware_sha: goodware},
        goodware=(goodware_sha,),
        manual_imports=(imported_positive,),
    )
    motif = "appDomainManagerAssembly"
    observation_ref = await _source_observation(harness, motif)

    _output, result_status, _observation_refs = await harness.rule_tools.propose(
        {
            "condition": "any",
            "motifs": [_motif(motif, observation_ref)],
        },
        harness.context,
    )

    assert result_status is ToolInvocationStatus.SUCCEEDED
    assert await harness.associations.list_for_investigation(harness.investigation_id) == ()
    rule_reports = [
        item
        for item in await harness.evidence.repository.list_for_investigation(
            harness.investigation_id
        )
        if item.source == "AutoWork RuleWorkbench"
    ]
    payload = await harness.evidence.read_json(rule_reports[-1].blob_id)
    assert payload["validation_status"] == "real_partial"
    assert payload["false_positive_count"] == 0
    assert payload["false_negative_count"] == 0
    assert payload["provenance_counts_by_role"]["confirmed_positive"] == {
        "real": 0,
        "synthetic": 1,
    }
    assert payload["provenance_counts_by_role"]["goodware"] == {"real": 1, "synthetic": 0}
    assert "positives: 1 synthetic" in payload["coverage_statement"].casefold()


@pytest.mark.asyncio
async def test_article_claim_rule_test_and_revise_supports_non_pe_files() -> None:
    pytest.importorskip("yara")
    from cti_app.domain.rule_workbench import (
        RuleCorpusInventoryItem,
        RuleSampleProvenance,
        RuleTestCorpusRole,
    )
    from cti_app.infrastructure.yara_runner import IsolatedYaraRunner

    positive = (
        b'<?xml version="1.0"?><configuration><runtime>'
        b'<etwEnable enabled="false"/><appDomainManagerAssembly '
        b'assemblyName="Telemetry" appDomainManagerType="Telemetry.Loader"/>'
        b"</runtime></configuration>"
    )
    benign = (
        b'<?xml version="1.0"?><configuration><runtime>'
        b'<gcServer enabled="true"/><assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1"/>'
        b"</runtime></configuration>"
    )
    positive_sha256 = _sha256(positive)
    benign_sha256 = _sha256(benign)
    imports = (
        RuleCorpusInventoryItem(
            sample_sha256=positive_sha256,
            role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
            provenance=RuleSampleProvenance.SYNTHETIC,
            size_bytes=len(positive),
            source_description="Synthetic .NET XML reflects the quoted article configuration.",
        ),
    )

    class ArticleClaims:
        calls = 0

        async def get_claims(self, investigation_id, claim_ids):
            self.calls += 1
            claims = (
                SimpleNamespace(
                    id="L1.1",
                    statement="The article fragment cites etwEnable enabled=false.",
                    source_refs=("article:paragraph:0004",),
                ),
                SimpleNamespace(
                    id="L1.2",
                    statement="The fragment names appDomainManagerAssembly and its Type.",
                    source_refs=("article:paragraph:0004",),
                ),
            )
            return tuple(claim for claim in claims if claim.id in claim_ids)

    article_claims = ArticleClaims()
    engine = IsolatedYaraRunner()
    if not engine.available:
        pytest.skip("the isolated YARA worker is unavailable in this environment")
    harness = _harness(
        samples={positive_sha256: positive, benign_sha256: benign},
        goodware=(benign_sha256,),
        manual_imports=imports,
        article_claims=article_claims,
        engine=engine,
    )
    assert await harness.associations.list_for_investigation(harness.investigation_id) == ()
    candidate_id = uuid4()
    motifs = [
        {
            "kind": "string",
            "value": "etwEnable",
            "provenance_kind": "manual",
            "provenance_id": "article-fragment:runtime-config",
            "article_claim_refs": ["L1.1"],
        },
        {
            "kind": "string",
            "value": "appDomainManagerAssembly",
            "provenance_kind": "manual",
            "provenance_id": "article-fragment:app-domain-manager",
            "article_claim_refs": ["L1.2"],
        },
    ]

    proposed = await _call(
        harness,
        "rule.propose",
        {"candidate_id": str(candidate_id), "motifs": motifs, "condition": "all"},
    )

    assert proposed.status is ToolInvocationStatus.SUCCEEDED
    assert proposed.output["report"]["validation_status"] == "real_partial"
    assert proposed.output["report"]["false_positive_count"] == 0
    assert proposed.output["report"]["false_negative_count"] == 0
    assert proposed.output["report"]["provenance_counts_by_role"] == {
        "confirmed_positive": {"real": 0, "synthetic": 1},
        "goodware": {"real": 1, "synthetic": 0},
        "out_of_cluster_candidate": {"real": 0, "synthetic": 0},
        "holdout": {"real": 0, "synthetic": 0},
    }
    assert "positives: 1 synthetic" in proposed.output["report"]["coverage_statement"]
    assert article_claims.calls == 1

    tested = await _call(harness, "rule.test", {"candidate_id": str(candidate_id)})
    assert tested.output["report"]["false_positive_count"] == 0
    assert tested.output["report"]["false_negative_count"] == 0
    assert tested.output["report"]["validation_status"] == "real_partial"

    revised = await _call(
        harness,
        "rule.revise",
        {
            "candidate_id": str(candidate_id),
            "parent_version": 1,
            "motifs": [motifs[0]],
        },
    )
    assert revised.output.get("version") == "v2", revised.output
    assert revised.output["report"]["validation_status"] == "real_partial"
    assert article_claims.calls == 1
    assert await harness.associations.list_for_investigation(harness.investigation_id) == ()


@pytest.mark.asyncio
async def test_unexpected_rule_operation_error_returns_stable_non_sensitive_reason(caplog) -> None:
    from cti_app.application.investigation_rule_tools import _RuleToolAdapter

    class ExplodingService:
        max_tool_seconds = 30

        async def execute(self, _tool_name, _arguments, _context):
            try:
                raise ValueError("database URL and operator payload must stay private")
            except ValueError as cause:
                raise RuntimeError("local operation failed") from cause

    context = ToolExecutionContext(
        investigation_id=uuid4(),
        envelope=None,
        budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 30}),
        idempotency_key="synthetic-rule-operation",
    )

    result = await _RuleToolAdapter(ExplodingService(), "rule.propose").execute({}, context)

    assert result.status is ToolInvocationStatus.FAILED
    assert result.output["reason"] == "local_rule_operation_failed:RuntimeError:ValueError"
    assert "database URL" not in str(result.output)
    assert "database URL" not in result.message
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_rule_with_only_synthetic_positive_cases_is_synthetic_only() -> None:
    from cti_app.domain.rule_workbench import (
        RuleCorpusInventoryItem,
        RuleSampleProvenance,
        RuleTestCorpusRole,
    )

    sample = b"<configuration><appDomainManagerAssembly /></configuration>"
    sample_sha256 = _sha256(sample)
    harness = _harness(
        samples={sample_sha256: sample},
        manual_imports=(
            RuleCorpusInventoryItem(
                sample_sha256=sample_sha256,
                role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
                provenance=RuleSampleProvenance.SYNTHETIC,
                size_bytes=len(sample),
                source_description="Synthetic XML from a cited article fragment.",
            ),
        ),
    )
    observation_ref = await _source_observation(harness, "appDomainManagerAssembly")

    result = await _call(
        harness,
        "rule.propose",
        {
            "candidate_id": str(uuid4()),
            "condition": "all",
            "motifs": [_motif("appDomainManagerAssembly", observation_ref)],
        },
    )

    assert result.output["report"]["validation_status"] == "synthetic_only"
    assert result.output["report"]["provenance_counts_by_role"]["confirmed_positive"] == {
        "real": 0,
        "synthetic": 1,
    }

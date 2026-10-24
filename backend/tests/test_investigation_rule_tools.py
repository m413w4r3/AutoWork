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

from cti_app.application.advisor_contracts import ADVISOR_SOURCE_PARSER_VERSION
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
    _report_summary,
    _RuleCorpusSnapshot,
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
    RuleRoleTestReport,
    RuleTestCase,
    RuleTestCorpusRole,
    RuleTestError,
    RuleTestErrorCode,
    RuleTestReport,
    YaraEngineResult,
    YaraExecutionStatus,
)

_EVIDENCE_NAMESPACE = UUID("47bfec4f-9070-42d0-aef5-0eb84c8b57cd")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_report_summary_without_corpus_uses_empty_corpus_fallback() -> None:
    report = RuleTestReport(
        source_sha256="a" * 64,
        execution_status=YaraExecutionStatus.OK,
        engine_version="fixture-yara",
        roles=tuple(
            RuleRoleTestReport(role=role, corpus_sha256s=()) for role in RuleTestCorpusRole
        ),
        selection_bias="No samples were available.",
        coverage_statement="No corpus coverage.",
    )

    summary = _report_summary(report, observation_id=None, corpus=None)

    assert summary["final_status"] == "ONE_RULE_NOT_VALIDATED"
    assert "qualified_positive_corpus_missing" in summary["validation_reasons"]


def test_report_summary_preserves_yara_reason_location_and_failing_role() -> None:
    role = RuleTestCorpusRole.HOLDOUT_NEGATIVE
    diagnostic = RuleTestError(
        code=RuleTestErrorCode.COMPILE_ERROR,
        message="syntax error, unexpected '}'",
        sample_sha256="b" * 64,
        line=4,
        column=1,
    )
    report = RuleTestReport(
        source_sha256="a" * 64,
        execution_status=YaraExecutionStatus.COMPILE_ERROR,
        engine_version="fixture-yara",
        roles=tuple(
            RuleRoleTestReport(
                role=current_role,
                corpus_sha256s=("b" * 64,) if current_role is role else (),
                test_cases=(
                    RuleTestCase(
                        sample_sha256="b" * 64,
                        role=role,
                        holdout_designated_at=datetime(2026, 10, 20, tzinfo=UTC),
                    ),
                )
                if current_role is role
                else (),
                errors=(diagnostic,) if current_role is role else (),
            )
            for current_role in RuleTestCorpusRole
        ),
        selection_bias="Fixture corpus.",
        coverage_statement="Fixture coverage.",
    )

    summary = _report_summary(report, observation_id=None, corpus=None)
    role_summary = next(item for item in summary["roles"] if item["role"] == role.value)
    diagnostic = role_summary["errors"][0]

    assert diagnostic == {
        "code": "compile_error",
        "message": "syntax error, unexpected '}'",
        "sample_sha256": "b" * 64,
        "line": 4,
        "column": 1,
        "role": "holdout_negative",
    }


@pytest.mark.asyncio
async def test_large_rule_report_is_persisted_as_a_complete_blob() -> None:
    artifact_store = _MemoryArtifactStore()
    harness = _harness(samples={}, artifact_store=artifact_store)
    role = RuleTestCorpusRole.DOCUMENTED_SEED_UNCONFIRMED
    cases = tuple(
        RuleTestCase(
            sample_sha256=hashlib.sha256(f"synthetic-report-case-{index}".encode()).hexdigest(),
            role=role,
        )
        for index in range(900)
    )
    hashes = tuple(case.sample_sha256 for case in cases)
    report = RuleTestReport(
        source_sha256="a" * 64,
        execution_status=YaraExecutionStatus.OK,
        engine_version="fixture-yara",
        roles=tuple(
            RuleRoleTestReport(
                role=current_role,
                corpus_sha256s=hashes if current_role is role else (),
                test_cases=cases if current_role is role else (),
                non_measurable=hashes if current_role is role else (),
            )
            for current_role in RuleTestCorpusRole
        ),
        selection_bias="Synthetic persistence fixture.",
        coverage_statement="All generated report rows are retained.",
    )
    corpus = _RuleCorpusSnapshot(
        cases=cases,
        selection_bias="Synthetic persistence fixture.",
        coverage_statement="All generated report rows are retained.",
        limitations=(),
    )

    observation = await harness.rule_tools._record_report(
        harness.context,
        report,
        corpus,
        candidate_id=uuid4(),
        version=None,
        candidate_status=DetectionCandidateStatus.CANDIDATE,
        operation="test",
    )

    encoded = artifact_store.values[observation.blob_id]
    persisted = json.loads(encoded)
    persisted_role = next(item for item in persisted["roles"] if item["role"] == role.value)
    assert len(encoded) > 131_072
    assert hashlib.sha256(encoded).hexdigest() == observation.sha256
    assert len(persisted_role["test_cases"]) == len(cases)
    assert len(persisted_role["non_measurable"]) == len(cases)
    assert persisted["summary"]["test_case_count"] == len(cases)


class _MemoryCorpusBytes:
    def __init__(self, samples: Mapping[str, bytes]) -> None:
        self.samples = dict(samples)

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        try:
            return self.samples[sample_sha256]
        except KeyError as exc:
            raise RuleCorpusBytesUnavailable("fixture bytes unavailable") from exc

    async def get_artifact_id_for_sample(self, sample_sha256: str) -> UUID:
        if sample_sha256 not in self.samples:
            raise RuleCorpusBytesUnavailable("fixture bytes unavailable")
        return uuid5(_EVIDENCE_NAMESPACE, f"corpus-artifact:{sample_sha256}")

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

    async def record_existing_blob(self, **kwargs: Any) -> EvidenceObservation:
        observation_id = (
            uuid5(_EVIDENCE_NAMESPACE, kwargs["idempotency_key"])
            if kwargs.get("idempotency_key")
            else uuid4()
        )
        observation = EvidenceObservation(
            id=observation_id,
            investigation_id=kwargs["investigation_id"],
            kind=kwargs["kind"],
            blob_id=kwargs["blob_id"],
            sha256=kwargs["sha256"],
            source=kwargs["source"],
            source_version=kwargs["source_version"],
            observed_at=kwargs.get("observed_at") or datetime.now(UTC),
            object_type=kwargs["object_type"],
            object_id=kwargs["object_id"],
            limitations=kwargs.get("limitations", ()),
            invocation_ref=kwargs.get("invocation_ref"),
        )
        existing = await self.repository.get(observation.id)
        if existing is not None:
            assert existing == observation
            return existing
        return await self.repository.append_if_absent(observation)

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
        holdout = tuple(
            RuleCorpusInventoryItem(
                sample_sha256=sample_sha256,
                role=RuleTestCorpusRole.HOLDOUT_POSITIVE,
                provenance=RuleSampleProvenance.REAL,
                size_bytes=len(self.samples.get(sample_sha256, b"")),
                source_description="Explicitly designated positive holdout fixture.",
                format_magic="legacy_untyped",
                designated_at=datetime(2026, 1, 1, tzinfo=UTC),
                observation_id=uuid5(_EVIDENCE_NAMESPACE, f"holdout:{sample_sha256}"),
                operator_id="analyst-test",
            )
            for sample_sha256 in self.holdout
        )
        return (*goodware, *holdout, *self._manual_imports)


class _MemoryArtifactStore:
    def __init__(self) -> None:
        self.values: dict[UUID, bytes] = {}

    async def put_canonical_json(self, payload: Mapping[str, Any], *, bucket: str):
        assert bucket == "investigation-evidence"
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        blob_id = uuid4()
        self.values[blob_id] = encoded
        return blob_id, hashlib.sha256(encoded).hexdigest()


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
    artifact_store: Any | None = None,
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
        artifact_store=artifact_store,
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
            "rule.propose_bridge",
            "rule.operator_syntax_fix",
            "rule.test",
            "rule.revise",
            "rule.compare",
            "rule.get",
        }
    )
    for name in (
        "rule.propose",
        "rule.propose_bridge",
        "rule.operator_syntax_fix",
        "rule.test",
        "rule.revise",
        "rule.compare",
        "rule.get",
    ):
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


async def _bridge_yara_observation(
    harness: _ToolHarness,
    *,
    source: str,
    measurement_catalog: Sequence[Mapping[str, Any]] | None = None,
    motifs: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[UUID, UUID, str]:
    motif_observation_id = await _source_observation(harness, "BridgeMeasuredMarker")
    motif_observation = await harness.evidence.repository.get(motif_observation_id)
    assert motif_observation is not None
    invocation_id = uuid4()
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    selected_catalog = list(
        measurement_catalog
        or (
            {
                "alias": "M1",
                "measurement_id": str(motif_observation.id),
                "sha256": "a" * 64,
                "sample_sha256": "1" * 64,
                "offset": 0,
                "length": len(b"BridgeMeasuredMarker"),
                "kind": "string",
                "encoding": "ascii",
                "value": "BridgeMeasuredMarker",
            },
        )
    )
    measurement_input_refs = [
        {"kind": "measurement", "id": row["measurement_id"], "sha256": row["sha256"]}
        for row in selected_catalog
    ]
    selected_motifs = list(
        motifs
        or (
            {
                "name": "$marker",
                "measurement_refs": [
                    {
                        "measurement_id": str(motif_observation.id),
                        "sha256": "a" * 64,
                    }
                ],
            },
        )
    )
    observation = await harness.evidence.record_json(
        investigation_id=harness.investigation_id,
        kind=EvidenceKind.OTHER,
        payload={
            "schema": "operator-advisor-call.v1",
            "consultation_type": "yara_draft",
            "advisor_response_schema": "advisor-response.v3",
            "advisor_status": "succeeded",
            "bridge_invocation_id": str(invocation_id),
            "input_refs": [
                {
                    "kind": "observation",
                    "id": str(motif_observation.id),
                    "sha256": motif_observation.sha256,
                },
                *measurement_input_refs,
            ],
            "measurement_catalog": selected_catalog,
            "output": {
                "schema": "advisor-response.v3",
                "consultation_type": "yara_draft",
                "status": "succeeded",
                "advisor_status": "succeeded",
                "usable": True,
                "bridge_provenance": {
                    "bridge_invocation_id": str(invocation_id),
                    "measurement_catalog": selected_catalog,
                },
                "rule_source": source,
                "rule_source_sha256": source_sha256,
                "motifs": selected_motifs,
            },
        },
        source="advisor.consult",
        source_version="advisor-response.v3",
        object_type="advisor_call",
        object_id="yara_draft",
        idempotency_key=f"bridge-yara:{invocation_id}",
    )
    return observation.id, invocation_id, source_sha256


@pytest.mark.asyncio
async def test_bridge_yara_proposal_rejects_motif_bytes_that_differ_from_measurement() -> None:
    harness = _harness(samples={})
    source = 'rule bridge_candidate { strings: $marker = "BridgeWrongMarker" condition: $marker }'
    observation_id, invocation_id, source_sha256 = await _bridge_yara_observation(
        harness, source=source
    )

    rejected = await _call(
        harness,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(observation_id),
            "bridge_invocation_id": str(invocation_id),
            "rule_source_sha256": source_sha256,
        },
    )

    assert rejected.status is ToolInvocationStatus.FAILED
    assert rejected.error_code == "motif_bytes_mismatch"
    assert rejected.output["diagnostic"]["motif"] == "$marker"
    assert rejected.output["diagnostic"]["measurement_alias"] == "M1"
    assert rejected.output["diagnostic"]["reason"] == "pattern_bytes_differ"


@pytest.mark.asyncio
async def test_bridge_yara_proposal_accepts_each_exact_hex_measurement() -> None:
    harness = _harness(samples={})
    source = """import "pe"
import "dotnet"
rule bridge_candidate {
    strings:
        $m2 = { EC CA DC C8 C2 8F C9 D5 }
        $m5 = { 19 44 4A 0A 01 01 18 08 }
    condition:
        pe.is_pe and dotnet.is_dotnet and $m2 and $m5
}
"""
    catalog = [
        {
            "alias": "M2",
            "measurement_id": "m002",
            "sha256": "d" * 64,
            "sample_sha256": "53f35e49eb9b271fd8cbcd3daacb525328dbf159a03dbd1c7adebe0363daa402",
            "offset": 14784,
            "length": 8,
            "kind": "hex",
            "encoding": "raw",
            "value": "eccadcc8c28fc9d5",
        },
        {
            "alias": "M5",
            "measurement_id": "m005",
            "sha256": "e" * 64,
            "sample_sha256": "53f35e49eb9b271fd8cbcd3daacb525328dbf159a03dbd1c7adebe0363daa402",
            "offset": 15232,
            "length": 8,
            "kind": "hex",
            "encoding": "raw",
            "value": "19444a0a01011808",
        },
    ]
    motifs = [
        {"name": "$m2", "measurement_refs": [{"measurement_id": "m002", "sha256": "d" * 64}]},
        {"name": "$m5", "measurement_refs": [{"measurement_id": "m005", "sha256": "e" * 64}]},
    ]
    observation_id, invocation_id, source_sha256 = await _bridge_yara_observation(
        harness, source=source, measurement_catalog=catalog, motifs=motifs
    )

    proposed = await _call(
        harness,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(observation_id),
            "bridge_invocation_id": str(invocation_id),
            "rule_source_sha256": source_sha256,
        },
    )

    assert proposed.status is ToolInvocationStatus.SUCCEEDED
    assert proposed.output["source_sha256"] == source_sha256


@pytest.mark.asyncio
async def test_rule_test_failure_persists_stage_reason_and_holdout_role() -> None:
    harness = _harness(samples={})
    source_ref = await _source_observation(harness, "RuleTestDiagnosticMarker")
    proposed = await _call(
        harness,
        "rule.propose",
        {
            "condition": "all",
            "motifs": [_motif("RuleTestDiagnosticMarker", source_ref)],
        },
    )

    async def fail_corpus(_investigation_id, *, tested_version):
        del tested_version
        raise ValueError("holdout cases require a persisted designation time")

    harness.rule_tools._build_corpus = fail_corpus
    failed = await _call(
        harness,
        "rule.test",
        {"candidate_id": proposed.output["candidate_id"]},
    )

    assert failed.status is ToolInvocationStatus.FAILED
    assert failed.error_code == "invalid_rule_input"
    assert failed.output["diagnostic"] == {
        "stage": "corpus_validation",
        "exception_class": "ValueError",
        "reason": "holdout cases require a persisted designation time",
        "role": "holdout",
    }


@pytest.mark.asyncio
async def test_bridge_yara_proposal_verifies_observation_source_and_operator_fix_is_versioned() -> (
    None
):
    harness = _harness(samples={})
    source = (
        'rule bridge_candidate { strings: $marker = "BridgeMeasuredMarker" condition: $marker }'
    )
    observation_id, invocation_id, source_sha256 = await _bridge_yara_observation(
        harness, source=source
    )

    rejected = await _call(
        harness,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(observation_id),
            "bridge_invocation_id": str(invocation_id),
            "rule_source_sha256": "b" * 64,
        },
    )
    assert rejected.status is ToolInvocationStatus.FAILED
    assert rejected.error_code == "bridge_yara_source_sha256_mismatch"

    proposed = await _call(
        harness,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(observation_id),
            "bridge_invocation_id": str(invocation_id),
            "rule_source_sha256": source_sha256,
        },
    )
    assert proposed.status is ToolInvocationStatus.SUCCEEDED
    assert proposed.output["source_author"] == "bridge"
    assert proposed.output["source_sha256"] == source_sha256
    candidate_id = proposed.output["candidate_id"]

    fixed_source = (
        'rule bridge_candidate {\n    strings:\n        $marker = "BridgeMeasuredMarker"\n'
        "    condition:\n        $marker\n}\n"
    )
    fixed = await _call(
        harness,
        "rule.operator_syntax_fix",
        {
            "candidate_id": candidate_id,
            "parent_version": 1,
            "source": fixed_source,
            "author": "operator:analyst-42",
        },
    )
    assert fixed.status is ToolInvocationStatus.SUCCEEDED
    assert fixed.output["version"] == "v2"
    assert fixed.output["source_author"] == "operator:analyst-42"
    assert fixed.output["source_change_kind"] == "operator_syntax_fix"
    assert fixed.output["source_diff"]

    history = await _call(
        harness, "rule.get", {"candidate_id": candidate_id, "include_history": True}
    )
    assert history.status is ToolInvocationStatus.SUCCEEDED
    assert history.output["versions"][0]["signature"]["source_attribution"]["author"] == "bridge"
    attribution = history.output["versions"][1]["signature"]["source_attribution"]
    assert attribution["author"] == "operator:analyst-42"
    assert attribution["change_kind"] == "operator_syntax_fix"
    assert "operator_diff" in attribution


@pytest.mark.asyncio
async def test_bridge_yara_proposal_accepts_only_linked_current_parser_reparse() -> None:
    harness = _harness(samples={})
    source = (
        'rule bridge_candidate { strings: $marker = "BridgeMeasuredMarker" condition: $marker }'
    )
    original_id, invocation_id, source_sha256 = await _bridge_yara_observation(
        harness, source=source
    )
    original = await harness.evidence.repository.get(original_id)
    assert original is not None
    original_payload = harness.evidence.payloads[original.blob_id]
    output_sha256 = "d" * 64
    original_payload["output_sha256"] = output_sha256
    original_output = original_payload["output"]
    original_output["output_sha256"] = output_sha256
    original_output["bridge_provenance"].update(
        output_sha256=output_sha256,
        input_refs=original_payload["input_refs"],
    )
    reparsed_payload = {
        **original_payload,
        "advisor_status": "succeeded",
        "output_sha256": output_sha256,
        "reparsed_from_observation_id": str(original.id),
        "parser_version": ADVISOR_SOURCE_PARSER_VERSION,
        "output": {
            **original_output,
            "status": "partial",
            "advisor_status": "succeeded",
            "output_sha256": output_sha256,
            "parser_version": ADVISOR_SOURCE_PARSER_VERSION,
            "bridge_provenance": {
                **original_output["bridge_provenance"],
                "bridge_invocation_id": str(invocation_id),
                "output_sha256": output_sha256,
                "input_refs": original_payload["input_refs"],
            },
        },
    }
    reparsed = await harness.evidence.record_json(
        investigation_id=harness.investigation_id,
        kind=EvidenceKind.OTHER,
        payload=reparsed_payload,
        source="advisor.consult",
        source_version=ADVISOR_SOURCE_PARSER_VERSION,
        object_type="advisor_call",
        object_id="yara_draft",
        idempotency_key="advisor-reparse-test",
    )

    proposed = await _call(
        harness,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(reparsed.id),
            "bridge_invocation_id": str(invocation_id),
            "rule_source_sha256": source_sha256,
        },
    )

    assert proposed.status is ToolInvocationStatus.SUCCEEDED
    assert proposed.output["bridge_observation_id"] == str(reparsed.id)
    assert proposed.output["bridge_invocation_id"] == str(invocation_id)
    assert proposed.output["source_author"] == "bridge"


@pytest.mark.asyncio
async def test_bridge_yara_proposal_rejects_truncated_source_and_two_rule_source() -> None:
    harness = _harness(samples={})
    source = (
        'rule first { strings: $marker = "BridgeMeasuredMarker" condition: $marker } '
        'rule second { strings: $other = "also-marker" condition: $other }'
    )
    observation_id, invocation_id, source_sha256 = await _bridge_yara_observation(
        harness, source=source
    )
    observation = await harness.evidence.repository.get(observation_id)
    assert observation is not None
    payload = harness.evidence.payloads[observation.blob_id]
    payload["output"]["truncation_status"] = "normalized_output_oversized"

    rejected = await _call(
        harness,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(observation_id),
            "bridge_invocation_id": str(invocation_id),
            "rule_source_sha256": source_sha256,
        },
    )
    assert rejected.status is ToolInvocationStatus.FAILED
    assert rejected.error_code == "bridge_yara_source_truncated"

    payload["output"].pop("truncation_status")
    two_rules = await _call(
        harness,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(observation_id),
            "bridge_invocation_id": str(invocation_id),
            "rule_source_sha256": source_sha256,
        },
    )
    assert two_rules.status is ToolInvocationStatus.FAILED
    assert "exactly_one_rule_required" in two_rules.error_code


@pytest.mark.asyncio
async def test_bridge_yara_proposal_requires_motif_measurement_ref_in_transmitted_inputs() -> None:
    harness = _harness(samples={})
    source = (
        'rule bridge_candidate { strings: $marker = "BridgeMeasuredMarker" condition: $marker }'
    )
    observation_id, invocation_id, source_sha256 = await _bridge_yara_observation(
        harness, source=source
    )
    observation = await harness.evidence.repository.get(observation_id)
    assert observation is not None
    payload = harness.evidence.payloads[observation.blob_id]
    payload["output"]["motifs"][0]["measurement_refs"][0]["sha256"] = "c" * 64

    rejected = await _call(
        harness,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(observation_id),
            "bridge_invocation_id": str(invocation_id),
            "rule_source_sha256": source_sha256,
        },
    )
    assert rejected.status is ToolInvocationStatus.FAILED
    assert rejected.error_code == "bridge_yara_measurement_ref_not_transmitted"


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
    assert result.error_code == "invalid_meta"
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

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["candidate_id"]
    assert result.output["version"] == "v1"
    assert result.output["validation_status"] == "untested"
    assert result.output["compile_check"]["status"] == "ok"
    assert "report" not in result.output


@pytest.mark.asyncio
async def test_rule_propose_creates_untested_candidate_with_empty_corpus_and_object_meta() -> None:
    harness = _harness(samples={})
    observation_ref = await _source_observation(harness, "EmptyCorpusMarker")

    proposed = await _call(
        harness,
        "rule.propose",
        {
            "condition": "all",
            "motifs": [_motif("EmptyCorpusMarker", observation_ref)],
            "meta": {"description": "compile only"},
        },
    )

    assert proposed.status is ToolInvocationStatus.SUCCEEDED
    assert proposed.output["candidate_id"]
    assert proposed.output["version"] == "v1"
    assert proposed.output["validation_status"] == "untested"
    assert proposed.output["candidate_status"] == "candidate"
    assert proposed.output["compile_check"]["status"] == "ok"
    assert harness.engine.match_count == 0
    versions = await harness.candidates.list_for_candidate(UUID(proposed.output["candidate_id"]))
    assert len(versions) == 1


@pytest.mark.asyncio
async def test_rule_cycle_requires_test_and_reports_only_explicit_test_results() -> None:
    from cti_app.domain.rule_workbench import (
        RuleCorpusInventoryItem,
        RuleSampleProvenance,
        RuleTestCorpusRole,
    )

    positive = b"NewMarker belongs to the documented positive fixture"
    goodware = b"ordinary benign configuration fixture"
    positive_sha256 = _sha256(positive)
    goodware_sha256 = _sha256(goodware)
    harness = _harness(
        samples={positive_sha256: positive, goodware_sha256: goodware},
        manual_imports=(
            RuleCorpusInventoryItem(
                sample_sha256=positive_sha256,
                role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
                provenance=RuleSampleProvenance.REAL,
                size_bytes=len(positive),
                source_description=(
                    "Inert unit-test bytes tagged real to exercise operator-import provenance."
                ),
            ),
            RuleCorpusInventoryItem(
                sample_sha256=goodware_sha256,
                role=RuleTestCorpusRole.GOODWARE,
                provenance=RuleSampleProvenance.REAL,
                size_bytes=len(goodware),
                source_description=(
                    "Inert unit-test bytes tagged real to exercise goodware provenance."
                ),
            ),
        ),
    )
    positive_association = await _associate(harness, positive_sha256, origin=SampleOrigin.MANUAL)
    await harness.associations.append_version(
        positive_association.change_qualification(
            SampleQualification.CONFIRMED,
            actor_kind=QualificationActorKind.HUMAN,
            actor_id="analyst-test",
            reason="This fixture verifies the qualified-positive revision gate.",
        ),
        expected_version=positive_association.version,
    )
    original_ref = await _source_observation(harness, "OriginalMarker")
    additional_ref = await _source_observation(harness, "NewMarker")
    proposed = await _call(
        harness,
        "rule.propose",
        {
            "condition": "all",
            "motifs": [_motif("OriginalMarker", original_ref)],
        },
    )
    candidate_id = proposed.output["candidate_id"]
    assert proposed.output["validation_status"] == "untested"
    assert harness.engine.match_count == 0

    revise_before_test = await _call(
        harness,
        "rule.revise",
        {
            "candidate_id": candidate_id,
            "parent_version": 1,
            "condition": "any",
            "motifs": [_motif("NewMarker", additional_ref)],
        },
    )
    assert revise_before_test.status is ToolInvocationStatus.FAILED
    assert revise_before_test.error_code == "rule_revision_requires_test"
    assert len(await harness.candidates.list_for_candidate(UUID(candidate_id))) == 1

    first_test = await _call(harness, "rule.test", {"candidate_id": candidate_id, "version": 1})
    assert first_test.output["report"]["false_negative_count"] == 1
    assert first_test.output["report"]["revision_recommendation"] == "revise_false_negative"

    revised = await _call(
        harness,
        "rule.revise",
        {
            "candidate_id": candidate_id,
            "parent_version": 1,
            "condition": "any",
            "motifs": [
                _motif("OriginalMarker", original_ref),
                _motif("NewMarker", additional_ref),
            ],
        },
    )
    assert revised.output["version"] == "v2"
    assert revised.output["validation_status"] == "untested"
    assert revised.output["compile_check"]["status"] == "ok"
    assert "report" not in revised.output

    second_test = await _call(harness, "rule.test", {"candidate_id": candidate_id, "version": 2})
    test_report = second_test.output["report"]
    assert test_report["false_negative_count"] == 0
    assert test_report["false_positive_count"] == 0
    assert test_report["rule_version_id"] == revised.output["version_id"]
    assert test_report["rule_source_sha256"] == revised.output["source_sha256"]
    assert test_report["final_status"] == "ONE_RULE_NOT_VALIDATED"
    assert test_report["corpus_sha256"]
    assert test_report["test_refs"]
    assert any(
        row["sha256"] == hashlib.sha256(positive).hexdigest()
        for role in test_report["roles"]
        for row in role["coverage_matrix"]
    )
    assert "confidence_interval_95" in test_report["metrics"]["real_positive_sensitivity"]
    assert second_test.output["validation_status"] == "real_partial"
    assert second_test.output["validation_reason"] == "sample_bytes_missing_or_unverified"
    assert second_test.output["report"]["revision_recommendation"] == "none_justified"
    unjustified_revision = await _call(
        harness,
        "rule.revise",
        {
            "candidate_id": candidate_id,
            "parent_version": 2,
            "condition": "any",
            "motifs": [
                _motif("OriginalMarker", original_ref),
                _motif("NewMarker", additional_ref),
                _motif("ThirdMarker", await _source_observation(harness, "ThirdMarker")),
            ],
        },
    )
    assert unjustified_revision.status is ToolInvocationStatus.FAILED
    assert unjustified_revision.error_code == "rule_revision_not_justified"
    assert "Aucune révision justifiée" in unjustified_revision.message


@pytest.mark.asyncio
async def test_unconfirmed_seed_matches_remain_candidates_not_family_positives() -> None:
    sample = b"unreviewed seed contains UnconfirmedMarker"
    sample_sha256 = _sha256(sample)
    holdout_sample = b"independent holdout contains UnconfirmedMarker"
    holdout_sha256 = _sha256(holdout_sample)
    harness = _harness(
        samples={sample_sha256: sample, holdout_sha256: holdout_sample},
        holdout=(holdout_sha256,),
    )
    await _associate(harness, sample_sha256, origin=SampleOrigin.SEED)
    observation_ref = await _source_observation(harness, "UnconfirmedMarker")
    proposed = await _call(
        harness,
        "rule.propose",
        {"condition": "all", "motifs": [_motif("UnconfirmedMarker", observation_ref)]},
    )

    tested = await _call(
        harness,
        "rule.test",
        {"candidate_id": proposed.output["candidate_id"], "version": 1},
    )
    report = tested.output["report"]
    positive = next(item for item in report["roles"] if item["role"] == "human_confirmed")
    candidate = next(
        item for item in report["roles"] if item["role"] == "documented_seed_unconfirmed"
    )
    holdout = next(item for item in report["roles"] if item["role"] == "holdout_positive")
    seed_result = candidate["test_cases"][0]

    assert not positive["test_cases"]
    assert candidate["matches"] == (sample_sha256,)
    assert holdout["matches"] == (holdout_sha256,)
    assert seed_result["expected_match"] is None
    assert seed_result["family_confirmation"] == "not_assessed"
    assert report["validation_status"] == "real_partial"
    assert report["false_positive_count"] == 0
    assert tested.output["candidate_status"] == DetectionCandidateStatus.CANDIDATE.value
    assert report["evaluation_protocol"]["holdout_roles"] == (
        "holdout_positive",
        "holdout_negative",
    )
    assert "does not confirm family" in report["evaluation_protocol"]["match_interpretation"]


@pytest.mark.asyncio
async def test_generic_dotnet_rule_requires_source_verified_all_of_motifs() -> None:
    sample = b"System.Runtime.InteropServices private marker RareMarkerQz"
    sample_sha256 = _sha256(sample)
    harness = _harness(samples={sample_sha256: sample})
    generic_ref = await _source_observation(harness, "System.Runtime.InteropServices")
    specific_ref = await _source_observation(harness, "RareMarkerQz")
    generic = _motif("System.Runtime.InteropServices", generic_ref)
    generic["sample_sha256"] = sample_sha256
    generic["offsets"] = [sample.index(b"System.Runtime.InteropServices")]
    specific = _motif("RareMarkerQz", specific_ref, provenance_id="source-located-family-marker")
    specific["sample_sha256"] = sample_sha256
    specific["offsets"] = [sample.index(b"RareMarkerQz")]
    meta = {
        "hypothesis": "The family uses a private payload marker alongside CLR metadata.",
        "dotnet_imports_discussion": (
            "The CLR namespace is common in benign software and is not family evidence."
        ),
    }
    rejected = await _call(
        harness,
        "rule.propose",
        {"condition": "all", "motifs": [generic], "meta": meta},
    )
    assert rejected.status is ToolInvocationStatus.FAILED
    assert rejected.error_code == "motif_validation_failed"

    accepted = await _call(
        harness,
        "rule.propose",
        {"condition": "all", "motifs": [generic, specific], "meta": meta},
    )
    assert accepted.status is ToolInvocationStatus.SUCCEEDED
    signature = accepted.output["signature"]
    assert signature["family"] is None
    assert signature["hypothesis"] == meta["hypothesis"]
    assert signature["source_provenance_complete"] is True
    assert all(item["source_bytes_verified"] for item in signature["motifs"])


@pytest.mark.asyncio
async def test_existing_generic_dotnet_signature_is_reported_as_unqualified_candidate() -> None:
    from cti_app.domain.investigation import DetectionCandidateVersion
    from cti_app.infrastructure.yara_runner import IsolatedYaraRunner

    sample = b"benign runtime references System.Runtime.InteropServices"
    sample_sha256 = _sha256(sample)
    harness = _harness(
        samples={sample_sha256: sample},
        goodware=(sample_sha256,),
        engine=IsolatedYaraRunner(),
    )
    source = """rule ExistingGenericDotnet {
    strings:
        $namespace = "System.Runtime.InteropServices" ascii wide
    condition:
        $namespace
}
"""
    version = DetectionCandidateVersion(
        investigation_id=harness.investigation_id,
        candidate_id=uuid4(),
        source=source,
        source_sha256=_sha256(source.encode()),
        motif_provenance=(),
    )
    await harness.candidates.append_version(version, expected_previous_id=None)

    tested = await _call(
        harness,
        "rule.test",
        {"candidate_id": str(version.candidate_id), "version": 1},
    )

    report = tested.output["report"]
    assert tested.output["candidate_status"] == DetectionCandidateStatus.REJECTED.value
    assert tested.output["validation_status"] == "real_partial"
    assert report["signature"]["generic_dotnet_import_count"] == 1
    assert report["signature"]["generic_dotnet_guard"] == "incomplete"
    assert report["metrics"]["real_goodware_false_positive_rate"]["numerator"] == 1
    assert report["metrics"]["real_positive_sensitivity"]["status"] == "not_calculable"
    assert any("source-byte-verified" in item for item in report["signature_limitations"])


@pytest.mark.asyncio
async def test_holdout_hash_overlapping_development_is_excluded() -> None:
    sample = b"one benign sample cannot be both development and holdout"
    sample_sha256 = _sha256(sample)
    harness = _harness(
        samples={sample_sha256: sample},
        goodware=(sample_sha256,),
        holdout=(sample_sha256,),
    )

    corpus = await harness.rule_tools._build_corpus(harness.investigation_id)

    assert sum(case.sample_sha256 == sample_sha256 for case in corpus.cases) == 2
    assert {case.role for case in corpus.cases} == {
        RuleTestCorpusRole.GOODWARE,
        RuleTestCorpusRole.HOLDOUT_POSITIVE,
    }
    assert corpus.holdout_overlap_sha256s == (sample_sha256,)


@pytest.mark.asyncio
async def test_rule_propose_returns_typed_compile_error_without_candidate() -> None:
    class InvalidSyntaxEngine(_DeterministicYaraEngine):
        def compile(self, source: str) -> YaraEngineResult:
            del source
            return YaraEngineResult(
                status=YaraExecutionStatus.COMPILE_ERROR,
                engine_version="fixture-yara-1",
                message="untrusted engine detail containing source",
                line=3,
            )

    harness = _harness(samples={}, engine=InvalidSyntaxEngine())
    observation_ref = await _source_observation(harness, "CompileFailureMarker")
    candidate_id = uuid4()

    result = await _call(
        harness,
        "rule.propose",
        {
            "candidate_id": str(candidate_id),
            "condition": "all",
            "motifs": [_motif("CompileFailureMarker", observation_ref)],
        },
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "rule_syntax_invalid"
    assert result.output["error_code"] == "rule_syntax_invalid"
    assert "line 3" in result.message
    assert "untrusted engine detail" not in str(result.output)
    assert await harness.candidates.list_for_candidate(candidate_id) == ()


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

    summary = _version_summary(
        version,
        (
            {
                "schema": "investigation-yara-test.v1",
                "operation": "propose",
                "source_sha256": version.source_sha256,
                "validation_status": "real_validated",
            },
        ),
    )

    assert summary["validation_status"] == "untested"
    assert summary["validation_reason"] == "explicit_rule_test_required"
    assert summary["latest_test_report"] is None
    assert summary["final_status"] == "ONE_RULE_NOT_VALIDATED"
    assert summary["validation_reasons"] == ["explicit_rule_test_required"]


def test_rule_version_summary_separates_latest_result_from_historical_crash() -> None:
    version = SimpleNamespace(
        id=uuid4(),
        candidate_id=uuid4(),
        investigation_id=uuid4(),
        version=1,
        version_label="v1",
        parent_version_id=None,
        source='rule candidate { strings: $marker = "marker" condition: $marker }',
        source_sha256="a" * 64,
        status=DetectionCandidateStatus.CANDIDATE,
        test_refs=(),
        motif_provenance=(),
    )
    reports = [
        {
            "operation": "test",
            "version_id": str(version.id),
            "rule_version_id": str(version.id),
            "source_sha256": version.source_sha256,
            "execution_status": "engine_crashed",
            "final_status": "ONE_RULE_NOT_VALIDATED",
            "validation_reasons": ["engine_not_ok"],
        },
        {
            "operation": "test",
            "version_id": str(version.id),
            "rule_version_id": str(version.id),
            "source_sha256": version.source_sha256,
            "execution_status": "ok",
            "final_status": "ONE_RULE_NOT_VALIDATED",
            "validation_reasons": ["qualified_positive_corpus_missing"],
        },
    ]

    summary = _version_summary(version, reports)

    assert summary["latest_test_report"]["execution_status"] == "ok"
    assert summary["final_status"] == "ONE_RULE_NOT_VALIDATED"
    assert summary["reports"] == reports


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
    assert result.output["versions"][0]["validation_reason"] == "explicit_rule_test_required"
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
    unavailable_candidate_id = uuid4()
    unavailable = await _call(
        unavailable_engine,
        "rule.propose",
        {
            "candidate_id": str(unavailable_candidate_id),
            "condition": "any",
            "motifs": [_motif("MarkerForTest", source_ref)],
        },
    )
    assert unavailable.status is ToolInvocationStatus.UNAVAILABLE
    assert unavailable.error_code == "rule_workbench_unavailable"
    assert await unavailable_engine.candidates.list_for_candidate(unavailable_candidate_id) == ()

    missing_bytes = _harness(samples={})
    missing_seed = await _associate(missing_bytes, sha256)
    confirmed_missing_seed = missing_seed.change_qualification(
        SampleQualification.CONFIRMED,
        actor_kind=QualificationActorKind.HUMAN,
        actor_id="analyst-test",
        reason="Fixture exercises missing-byte reporting for a qualified positive.",
    )
    await missing_bytes.associations.append_version(
        confirmed_missing_seed, expected_version=missing_seed.version
    )
    source_ref = await _source_observation(missing_bytes, "MarkerForTest")
    proposed = await _call(
        missing_bytes,
        "rule.propose",
        {"condition": "any", "motifs": [_motif("MarkerForTest", source_ref)]},
    )
    assert proposed.status is ToolInvocationStatus.SUCCEEDED
    assert proposed.output["validation_status"] == "untested"
    non_measurable = await _call(
        missing_bytes,
        "rule.test",
        {"candidate_id": proposed.output["candidate_id"]},
    )
    assert non_measurable.status is ToolInvocationStatus.UNAVAILABLE
    report_ref = non_measurable.observation_refs[0]
    observation = await missing_bytes.evidence.repository.get(report_ref)
    assert observation is not None
    assert observation.kind is EvidenceKind.YARA_MATCH
    report = missing_bytes.evidence.payloads[observation.blob_id]
    positive = next(role for role in report["roles"] if role["role"] == "bytes_missing")
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
    first_seed = await _associate(harness, first_sha, origin=SampleOrigin.SEED)
    confirmed_first_seed = first_seed.change_qualification(
        SampleQualification.CONFIRMED,
        actor_kind=QualificationActorKind.HUMAN,
        actor_id="analyst-test",
        reason="Synthetic fixture is explicitly qualified for this test.",
    )
    await harness.associations.append_version(
        confirmed_first_seed, expected_version=first_seed.version
    )
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
    assert v1_result.output["candidate_status"] == DetectionCandidateStatus.CANDIDATE.value
    assert v1_result.output["validation_status"] == "untested"
    v1 = (await harness.candidates.list_for_candidate(candidate_id))[0]
    assert v1.status is DetectionCandidateStatus.CANDIDATE

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

    v1_test = await _call(
        harness,
        "rule.test",
        {"candidate_id": str(candidate_id), "version": 1},
    )
    assert v1_test.output["report"]["false_negative_count"] == 2
    assert v1_test.output["report"]["revision_recommendation"] == "revise_false_negative"

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
    assert v2_result.output["validation_status"] == "untested"
    v2 = (await harness.candidates.list_for_candidate(candidate_id))[-1]
    assert v2.parent_version_id == v1.id
    assert v2.status is DetectionCandidateStatus.CANDIDATE

    v2_test = await _call(
        harness,
        "rule.test",
        {"candidate_id": str(candidate_id), "version": 2},
    )
    assert v2_test.output["report"]["false_negative_count"] == 1
    assert v2_test.output["report"]["revision_recommendation"] == "none_justified"

    compared = await _call(
        harness,
        "rule.compare",
        {"candidate_id": str(candidate_id), "version_a": 1, "version_b": 2},
    )
    assert compared.status is ToolInvocationStatus.PARTIAL
    positive_delta = next(
        role for role in compared.output["roles"] if role["role"] == "human_confirmed"
    )
    assert positive_delta["matches_gained"] == (second_sha,)
    assert compared.output["finding_candidate"]["kind"] == "detection"
    comparison_observation = await harness.evidence.repository.get(compared.observation_refs[0])
    assert comparison_observation is not None
    assert comparison_observation.kind is EvidenceKind.YARA_MATCH

    v1_observation = await harness.evidence.repository.get(v1_test.observation_refs[0])
    assert v1_observation is not None and v1_observation.kind is EvidenceKind.YARA_MATCH
    v1_payload = harness.evidence.payloads[v1_observation.blob_id]
    role_hashes = v1_payload["corpus_sha256s_by_role"]
    assert role_hashes["human_confirmed"] == sorted([first_sha, second_sha])
    assert role_hashes["goodware"] == [goodware_sha]
    assert role_hashes["holdout_positive"] == [holdout_sha]
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
    unclassified_holdout_sha = "f" * 64

    async def legacy_holdout_hashes(_investigation_id: UUID) -> Sequence[str]:
        return (unclassified_holdout_sha,)

    harness.bytes_port.holdout_sample_sha256s = legacy_holdout_hashes
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
    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert second == first
    assert harness.engine.compile_count == before + 1
    # Informational corpus gaps remain visible and keep validation quality partial,
    # while the completed measurement itself succeeds.
    assert first.output["report"]["validation_status"] == "real_partial"
    assert first.output["report"]["candidate_status"] == "candidate"
    assert any(
        "holdout sans rôle" in limitation for limitation in first.output["report"]["limitations"]
    )
    corpus = await harness.rule_tools._build_corpus(harness.investigation_id)
    assert not corpus.blocking_limitations
    assert any("holdout sans rôle" in item for item in corpus.measurement_limitations)
    assert any("Aucun fichier binaire goodware" in item for item in corpus.measurement_limitations)
    assert any("goodware_baseline" in item for item in corpus.measurement_limitations)
    assert proposed.status is ToolInvocationStatus.SUCCEEDED
    assert proposed.output["validation_status"] == "untested"


@pytest.mark.asyncio
async def test_blocking_corpus_limit_makes_measured_rule_partial_and_candidate() -> None:
    samples = {
        _sha256(b"first confirmed SharedBlockingMarker"): b"first confirmed SharedBlockingMarker",
        _sha256(b"second confirmed SharedBlockingMarker"): b"second confirmed SharedBlockingMarker",
    }
    harness = _harness(samples=samples)
    for sample_sha256 in samples:
        association = await _associate(
            harness,
            sample_sha256,
        )
        confirmed_association = association.change_qualification(
            SampleQualification.CONFIRMED,
            actor_kind=QualificationActorKind.HUMAN,
            actor_id="analyst-test",
            reason="Confirm the synthetic positive fixture for the blocking-limit test.",
        )
        await harness.associations.append_version(
            confirmed_association,
            expected_version=association.version,
        )
    ref = await _source_observation(harness, "SharedBlockingMarker")
    proposed = await _call(
        harness,
        "rule.propose",
        {
            "candidate_id": str(uuid4()),
            "condition": "any",
            "motifs": [_motif("SharedBlockingMarker", ref)],
        },
    )

    async def unreadable_legacy_holdouts(_investigation_id: UUID) -> Sequence[str]:
        raise RuntimeError("fixture reader failure")

    harness.bytes_port.holdout_sample_sha256s = unreadable_legacy_holdouts
    tested = await _call(
        harness,
        "rule.test",
        {"candidate_id": proposed.output["candidate_id"]},
    )

    assert tested.status is ToolInvocationStatus.PARTIAL
    assert tested.output["report"]["candidate_status"] == "candidate"
    assert any(
        "fournisseur de hash holdout n'a pas pu être lu" in item
        for item in tested.output["report"]["limitations"]
    )
    corpus = await harness.rule_tools._build_corpus(harness.investigation_id)
    assert any("fournisseur de hash holdout" in item for item in corpus.blocking_limitations)
    assert any("goodware_baseline" in item for item in corpus.measurement_limitations)


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
    assert result.output["validation_status"] == "untested"
    assert version.status is DetectionCandidateStatus.CANDIDATE
    tested = await _call(harness, "rule.test", {"candidate_id": str(candidate)})
    assert tested.output["candidate_status"] == DetectionCandidateStatus.REJECTED.value
    assert tested.output["report"]["false_positive_count"] == 1
    assert harness.registry.get("rule.approve", "1") is None
    assert not any(
        item.status is DetectionCandidateStatus.APPROVED
        for item in await harness.candidates.list_for_candidate(candidate)
    )


@pytest.mark.asyncio
async def test_noisy_rule_does_not_block_real_loop_opinion_draft() -> None:
    from tests.test_investigation_service import (
        _decision,
        _finding_id,
        _Fixture,
        _operation_and_observation_id,
        _opinion_delta,
        _proposal,
        _tool_call,
    )

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
    rule_test_call = {
        "tool_name": "rule.test",
        "schema_version": "1",
        "arguments": {"candidate_id": str(candidate_id), "version": 1},
        "purpose": "measure the exact proposed candidate version against its corpus",
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
                goal="test the exact candidate version before finishing",
                next_action="continue",
                tools=(rule_test_call,),
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
                        "candidate was rejected by its explicit test."
                    ),
                ),
            ),
        ]
    )
    fixture.bridge = BridgeInvestigator(fixture.bridge_transport)
    fixture.service._bridge = fixture.bridge

    result = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    rule_test_invocations = tuple(item for item in invocations if item.tool_name == "rule.test")
    assert len(rule_test_invocations) == 1, invocations
    assert rule_test_invocations[0].status in {
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.PARTIAL,
    }
    rejected = (await candidates.list_for_candidate(candidate_id))[0]
    assert rejected.status is DetectionCandidateStatus.CANDIDATE
    test_reports = tuple(
        payload
        for payload in evidence.payloads.values()
        if payload.get("operation") == "test" and payload.get("candidate_id") == str(candidate_id)
    )
    assert len(test_reports) == 1
    assert test_reports[0]["candidate_status"] == DetectionCandidateStatus.REJECTED.value
    assert test_reports[0]["false_positive_count"] == 1
    assert result.stop_reason.value == "agent_finished"
    opinions = list(fixture.opinions._items.values())
    assert len(opinions) == 1
    assert opinions[0].status.value == "draft"
    assert opinions[0].blocks
    assert opinions[0].blocks[0].finding_refs == (finding_id,)
    assert await fixture.observations.get(chain_observation_id) is not None


@pytest.mark.asyncio
async def test_investigation_chain_can_finish_without_any_rule_tool() -> None:
    from tests.test_investigation_service import (
        _decision,
        _finding_id,
        _Fixture,
        _operation_and_observation_id,
        _opinion_delta,
        _proposal,
        _tool_call,
    )

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
    assert "aucun fichier binaire goodware" in " ".join(corpus.limitations).casefold()


@pytest.mark.asyncio
async def test_conflicting_corpus_roles_and_unreadable_goodware_baseline_are_blocking() -> None:
    from cti_app.domain.rule_workbench import (
        RuleCorpusInventoryItem,
        RuleSampleProvenance,
        RuleTestCorpusRole,
    )

    content = b"synthetic sample with conflicting corpus roles"
    sample_sha256 = _sha256(content)
    imports = tuple(
        RuleCorpusInventoryItem(
            sample_sha256=sample_sha256,
            role=role,
            provenance=RuleSampleProvenance.SYNTHETIC,
            size_bytes=len(content),
            source_description="Synthetic conflicting-role classification fixture.",
        )
        for role in (RuleTestCorpusRole.GOODWARE, RuleTestCorpusRole.HARD_NEGATIVE)
    )
    harness = _harness(samples={sample_sha256: content}, manual_imports=imports)

    async def unreadable_baseline(_investigation_id: UUID) -> UUID | None:
        raise RuntimeError("fixture baseline reader failure")

    harness.bytes_port.goodware_baseline_id = unreadable_baseline
    corpus = await harness.rule_tools._build_corpus(harness.investigation_id)

    assert any("plusieurs rôles de corpus" in item for item in corpus.blocking_limitations)
    assert any("baseline goodware liée" in item for item in corpus.blocking_limitations)
    assert not any("n'est liée" in item for item in corpus.measurement_limitations)
    assert any("plusieurs rôles de corpus" in item for item in corpus.limitations)


@pytest.mark.asyncio
async def test_manual_import_role_takes_precedence_over_its_candidate_association() -> None:
    from cti_app.domain.rule_workbench import (
        RuleCorpusInventoryItem,
        RuleSampleProvenance,
        RuleTestCorpusRole,
    )

    content = b"operator-imported benign corpus fixture"
    sample_sha256 = _sha256(content)
    manual_import = RuleCorpusInventoryItem(
        sample_sha256=sample_sha256,
        role=RuleTestCorpusRole.GOODWARE,
        provenance=RuleSampleProvenance.SYNTHETIC,
        size_bytes=len(content),
        source_description="Synthetic operator import used by this test.",
    )
    harness = _harness(samples={sample_sha256: content}, manual_imports=(manual_import,))
    await _associate(harness, sample_sha256, origin=SampleOrigin.MANUAL)

    corpus = await harness.rule_tools._build_corpus(harness.investigation_id)

    assert len(corpus.cases) == 1
    assert corpus.cases[0].sample_sha256 == sample_sha256
    assert corpus.cases[0].role is RuleTestCorpusRole.GOODWARE


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

    proposed = await _call(
        harness,
        "rule.propose",
        {"condition": "any", "motifs": [_motif(motif, observation_ref)]},
    )
    assert proposed.status is ToolInvocationStatus.SUCCEEDED
    tested = await _call(
        harness,
        "rule.test",
        {"candidate_id": proposed.output["candidate_id"]},
    )
    assert tested.output["validation_status"] == "real_partial"
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
    assert payload["provenance_counts_by_role"]["new_candidate_unconfirmed"] == {
        "real": 0,
        "synthetic": 1,
    }
    assert payload["provenance_counts_by_role"]["goodware"] == {"real": 1, "synthetic": 0}
    assert "seeds/candidats non confirmés" in payload["coverage_statement"].casefold()


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
    assert proposed.output["validation_status"] == "untested"
    assert proposed.output["compile_check"]["status"] == "ok"
    assert "report" not in proposed.output
    assert article_claims.calls == 1

    tested = await _call(harness, "rule.test", {"candidate_id": str(candidate_id)})
    assert tested.output["report"]["false_positive_count"] == 0
    assert tested.output["report"]["false_negative_count"] == 0
    assert tested.output["report"]["validation_status"] == "real_partial"
    assert tested.output["report"]["revision_recommendation"] == "none_justified"
    assert "positives: none" in tested.output["report"]["coverage_statement"]

    revised = await _call(
        harness,
        "rule.revise",
        {
            "candidate_id": str(candidate_id),
            "parent_version": 1,
            "motifs": [motifs[0]],
        },
    )
    assert revised.status is ToolInvocationStatus.FAILED
    assert revised.error_code == "rule_revision_not_justified"
    assert "Aucune révision justifiée" in revised.message
    assert article_claims.calls == 1
    assert len(await harness.candidates.list_for_candidate(candidate_id)) == 1
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
    assert result.output["error_code"] == "local_rule_operation_failed"
    assert result.error_code == "local_rule_operation_failed"
    assert result.exception_class == "RuntimeError:ValueError"
    assert "database URL" not in str(result.output)
    assert "database URL" not in result.message
    assert "Traceback" not in caplog.text
    assert caplog.records[-1].event == "investigation_tool_rule_operation_failure"
    assert caplog.records[-1].exception_class == "RuntimeError:ValueError"


@pytest.mark.asyncio
async def test_registry_dispatch_returns_bounded_rule_failure_code_and_cause(caplog) -> None:
    harness = _harness(samples={})
    observation_ref = await _source_observation(harness, "DispatcherFailureMarker")

    async def fail_candidate_lookup(_candidate_id):
        try:
            raise ValueError("private database URL and host path")
        except ValueError as cause:
            raise RuntimeError("local operation failed") from cause

    harness.candidates.list_for_candidate = fail_candidate_lookup
    result = await _call(
        harness,
        "rule.propose",
        {
            "condition": "all",
            "motifs": [_motif("DispatcherFailureMarker", observation_ref)],
        },
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "local_rule_operation_failed"
    assert result.output["error_code"] == "local_rule_operation_failed"
    assert result.output["reason"] == "local_rule_operation_failed:RuntimeError:ValueError"
    assert "database URL" not in result.message
    assert "private database URL" not in str(result.output)
    assert "Traceback" not in caplog.text
    failure_log = next(
        record
        for record in reversed(caplog.records)
        if getattr(record, "event", None) == "investigation_tool_rule_operation_failure"
    )
    assert failure_log.error_code == "local_rule_operation_failed"
    assert failure_log.exception_class == "RuntimeError:ValueError"


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

    proposed = await _call(
        harness,
        "rule.propose",
        {
            "candidate_id": str(uuid4()),
            "condition": "all",
            "motifs": [_motif("appDomainManagerAssembly", observation_ref)],
        },
    )

    assert proposed.output["validation_status"] == "untested"
    tested = await _call(
        harness,
        "rule.test",
        {"candidate_id": proposed.output["candidate_id"]},
    )
    assert tested.output["report"]["validation_status"] == "synthetic_only"
    assert tested.output["report"]["provenance_counts_by_role"]["new_candidate_unconfirmed"] == {
        "real": 0,
        "synthetic": 1,
    }

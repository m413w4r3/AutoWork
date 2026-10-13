"""Optional, local YARA tools exposed to the investigation tool registry."""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4, uuid5

from cti_app.application.blob_storage import (
    BlobReadLimitExceededError,
    BlobStorageUnavailableError,
    BlobStore,
)
from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.investigation_persistence import (
    DetectionCandidateRepository,
    EvidenceObservationRepository,
    SampleAssociationRepository,
)
from cti_app.application.investigation_tools import (
    ToolAdapter,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.rule_workbench import (
    RuleCorpusBytesPort,
    RuleCorpusBytesUnavailable,
    RuleWorkbench,
)
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.domain.analysis import SampleFeatureSetV1
from cti_app.domain.code_features import CodeFeatureSet
from cti_app.domain.goodware import GoodwareFeature
from cti_app.domain.invariants import CandidateInvariant
from cti_app.domain.investigation import (
    BudgetDimension,
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
    MotifProvenance,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.rule_workbench import (
    RuleConditionStrategy,
    RuleMotif,
    RuleMotifKind,
    RuleMotifProvenanceKind,
    RuleRoleTestReport,
    RuleTestCase,
    RuleTestCorpusRole,
    RuleTestReport,
    RuleVersionComparison,
)

JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"
RULE_WORKBENCH_CAPABILITY = "rule.workbench"
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SAFE_TEXT_PATTERN = r"^(?!.*[\x00-\x1f\x7f])[\s\S]*$"
_UUID_NAMESPACE = UUID("a24a70a5-f901-451a-8969-a0ad0ea0b267")
_MAX_HISTORY = 64
_PERSISTED_MOTIF_PREFIX = "rule-workbench-motif-v1:"


def _schema(
    properties: Mapping[str, Any],
    required: Sequence[str] = (),
    *,
    constraints: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "$schema": JSON_SCHEMA_2020_12,
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(required),
    }
    if constraints:
        result.update(constraints)
    return result


_MOTIF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": [item.value for item in RuleMotifKind]},
        "value": {
            "type": "string",
            "minLength": 1,
            "maxLength": 512,
            "pattern": _SAFE_TEXT_PATTERN,
        },
        "provenance_kind": {
            "type": "string",
            "enum": [item.value for item in RuleMotifProvenanceKind],
        },
        "provenance_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": _SAFE_TEXT_PATTERN,
        },
        "observation_refs": {
            "type": "array",
            "items": {"type": "string", "pattern": _UUID_PATTERN},
            "minItems": 1,
            "maxItems": 16,
            "uniqueItems": True,
        },
        "sample_sha256": {"type": "string", "pattern": _SHA256_PATTERN},
        "offsets": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": True,
        },
        "rationale": {"type": "string", "maxLength": 1_000, "pattern": _SAFE_TEXT_PATTERN},
        "positive_support": {"type": "integer", "minimum": 0},
        "benign_prevalence": {"type": "integer", "minimum": 0},
        "goodware_occurrence_count": {"type": "integer", "minimum": 0},
    },
    "required": ["kind", "value", "provenance_kind", "provenance_id", "observation_refs"],
}

RULE_PROPOSE_SCHEMA = _schema(
    {
        "motifs": {"type": "array", "items": _MOTIF_SCHEMA, "minItems": 1, "maxItems": 128},
        "feature_refs": {
            "type": "array",
            "items": {"type": "string", "pattern": _UUID_PATTERN},
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": True,
        },
        "candidate_id": {"type": "string", "pattern": _UUID_PATTERN},
        "condition": {"type": "string", "enum": [item.value for item in RuleConditionStrategy]},
        "meta": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "classification": {
                    "type": "string",
                    "maxLength": 128,
                    "pattern": _SAFE_TEXT_PATTERN,
                },
                "family": {"type": "string", "maxLength": 128, "pattern": _SAFE_TEXT_PATTERN},
                "description": {"type": "string", "maxLength": 512, "pattern": _SAFE_TEXT_PATTERN},
                "source": {"type": "string", "maxLength": 128, "pattern": _SAFE_TEXT_PATTERN},
            },
        },
    },
    required=("condition",),
)

RULE_TEST_SCHEMA = _schema(
    {
        "candidate_id": {"type": "string", "pattern": _UUID_PATTERN},
        "version": {"type": "integer", "minimum": 1},
    },
    ("candidate_id",),
)

RULE_REVISE_SCHEMA = _schema(
    {
        "candidate_id": {"type": "string", "pattern": _UUID_PATTERN},
        "parent_version": {"type": "integer", "minimum": 1},
        "motifs": {"type": "array", "items": _MOTIF_SCHEMA, "minItems": 1, "maxItems": 128},
        "condition": {"type": "string", "enum": [item.value for item in RuleConditionStrategy]},
        "meta": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "classification": {
                    "type": "string",
                    "maxLength": 128,
                    "pattern": _SAFE_TEXT_PATTERN,
                },
                "family": {"type": "string", "maxLength": 128, "pattern": _SAFE_TEXT_PATTERN},
                "description": {"type": "string", "maxLength": 512, "pattern": _SAFE_TEXT_PATTERN},
                "source": {"type": "string", "maxLength": 128, "pattern": _SAFE_TEXT_PATTERN},
            },
        },
    },
    ("candidate_id", "parent_version"),
)

RULE_COMPARE_SCHEMA = _schema(
    {
        "candidate_id": {"type": "string", "pattern": _UUID_PATTERN},
        "version_a": {"type": "integer", "minimum": 1},
        "version_b": {"type": "integer", "minimum": 1},
    },
    ("candidate_id", "version_a", "version_b"),
)

RULE_GET_SCHEMA = _schema(
    {
        "candidate_id": {"type": "string", "pattern": _UUID_PATTERN},
        "version": {"type": "integer", "minimum": 1},
        "include_history": {"type": "boolean"},
    },
    ("candidate_id",),
)

_TOOL_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "rule.propose": RULE_PROPOSE_SCHEMA,
    "rule.test": RULE_TEST_SCHEMA,
    "rule.revise": RULE_REVISE_SCHEMA,
    "rule.compare": RULE_COMPARE_SCHEMA,
    "rule.get": RULE_GET_SCHEMA,
}
_TOOL_CAPABILITY_KEYS = {name: name for name in _TOOL_SCHEMAS}


class EvidencePayloadReader(Protocol):
    """Read the canonical JSON payload behind a recorded evidence blob."""

    async def read_json(self, blob_id: UUID) -> dict[str, Any]: ...


@runtime_checkable
class RuleCorpusInventoryPort(Protocol):
    async def goodware_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]: ...

    async def holdout_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class RuleFeatureInputs:
    invariants: tuple[CandidateInvariant, ...] = ()
    feature_sets: tuple[SampleFeatureSetV1, ...] = ()
    code_feature_sets: tuple[CodeFeatureSet, ...] = ()
    sample_sha256_by_id: Mapping[UUID, str] = field(default_factory=dict)
    p10_motifs: tuple[RuleMotif, ...] = ()
    observation_refs_by_invariant: Mapping[UUID, tuple[UUID, ...]] = field(default_factory=dict)
    goodware_features: tuple[GoodwareFeature, ...] = ()


class RuleFeatureProvider(Protocol):
    async def load_rule_features(
        self, investigation_id: UUID, feature_refs: Sequence[UUID]
    ) -> RuleFeatureInputs: ...


class _EmptyRuleCorpusInventory:
    async def goodware_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        del investigation_id
        return ()

    async def holdout_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        del investigation_id
        return ()


class QuarantineRuleCorpusBytes(RuleCorpusBytesPort):
    """Resolve exact sample bytes from quarantine and bound goodware source blobs."""

    _READABLE_BUCKETS = (SAMPLES_QUARANTINE_BUCKET, "goodware-baselines")

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        blob_store: BlobStore,
        *,
        maximum_sample_bytes: int = 64 * 1024 * 1024,
        holdout_sample_provider: Callable[[UUID], Awaitable[Sequence[str]]] | None = None,
    ) -> None:
        if maximum_sample_bytes < 1:
            raise ValueError("maximum_sample_bytes must be positive")
        self._uow_factory = uow_factory
        self._blob_store = blob_store
        self._maximum_sample_bytes = maximum_sample_bytes
        self._holdout_sample_provider = holdout_sample_provider

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        if (
            not isinstance(sample_sha256, str)
            or re.fullmatch(_SHA256_PATTERN, sample_sha256) is None
        ):
            raise RuleCorpusBytesUnavailable("sample SHA-256 is invalid")
        record = None
        async with self._uow_factory() as uow:
            for bucket in self._READABLE_BUCKETS:
                record = await uow.blobs.get_by_address(bucket, sample_sha256)
                if record is not None:
                    break
        if record is None:
            raise RuleCorpusBytesUnavailable("sample bytes are not present in readable storage")
        descriptor = record.descriptor
        if descriptor.logical_bucket not in self._READABLE_BUCKETS:
            raise RuleCorpusBytesUnavailable("sample bytes are outside readable storage")
        if descriptor.sha256 != sample_sha256:
            raise ValueError("quarantined sample metadata does not match its requested SHA-256")
        if descriptor.size > self._maximum_sample_bytes:
            raise RuleCorpusBytesUnavailable("sample exceeds the bounded read size")
        try:
            payload = await self._blob_store.read(descriptor, max_bytes=self._maximum_sample_bytes)
        except BlobReadLimitExceededError as exc:
            raise RuleCorpusBytesUnavailable("sample exceeds the bounded read size") from exc
        except FileNotFoundError as exc:
            raise RuleCorpusBytesUnavailable("sample bytes are no longer available") from exc
        except BlobStorageUnavailableError:
            raise
        if (
            not isinstance(payload, bytes)
            or len(payload) != descriptor.size
            or hashlib.sha256(payload).hexdigest() != sample_sha256
        ):
            raise ValueError("quarantined sample bytes failed their size or SHA-256 check")
        return payload

    async def goodware_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        async with self._uow_factory() as uow:
            baseline_id = await uow.investigation_goodware_baselines.get(investigation_id)
            if baseline_id is None:
                return ()
            baseline = await uow.goodware_baselines.get(baseline_id)
        if baseline is None:
            return ()
        return tuple(sorted({source.sha256 for source in baseline.sources}))

    async def holdout_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        if self._holdout_sample_provider is None:
            return ()
        return tuple(await self._holdout_sample_provider(investigation_id))


@dataclass(frozen=True, slots=True)
class _RuleCorpusSnapshot:
    cases: tuple[RuleTestCase, ...]
    selection_bias: str
    coverage_statement: str
    limitations: tuple[str, ...]

    def hashes_by_role(self) -> dict[RuleTestCorpusRole, tuple[str, ...]]:
        return {
            role: tuple(sorted({case.sample_sha256 for case in self.cases if case.role is role}))
            for role in RuleTestCorpusRole
        }


@dataclass(frozen=True, slots=True)
class _CreatedRuleVersion:
    candidate_id: UUID
    version: DetectionCandidateVersion | None
    report: RuleTestReport
    observation: EvidenceObservation
    status: ToolInvocationStatus
    candidate_status: DetectionCandidateStatus
    corpus: _RuleCorpusSnapshot


class _ToolUnavailable(RuntimeError):
    pass


class _ToolFailed(RuntimeError):
    pass


def rule_tool_capabilities(
    *,
    workbench: RuleWorkbench | None,
    candidates: DetectionCandidateRepository | None = None,
    associations: SampleAssociationRepository | None = None,
    corpus_bytes: RuleCorpusBytesPort | None = None,
    recorder: EvidenceRecorder | None = None,
    evidence_observations: EvidenceObservationRepository | None = None,
    evidence_payloads: EvidencePayloadReader | None = None,
    feature_provider: RuleFeatureProvider | None = None,
) -> frozenset[str]:
    """Project injected rule ports onto the registry's base and per-tool keys."""
    capabilities: set[str] = set()
    if workbench is not None:
        capabilities.add(RULE_WORKBENCH_CAPABILITY)
    if workbench is not None and all(
        port is not None for port in (candidates, associations, corpus_bytes, recorder)
    ):
        capabilities.update(
            _TOOL_CAPABILITY_KEYS[name] for name in ("rule.test", "rule.revise", "rule.compare")
        )
        capabilities.add(_TOOL_CAPABILITY_KEYS["rule.propose"])
    if all(port is not None for port in (candidates, evidence_observations, evidence_payloads)):
        capabilities.add(_TOOL_CAPABILITY_KEYS["rule.get"])
    return frozenset(capabilities)


def register_rule_tools(
    registry: ToolRegistry,
    *,
    workbench: RuleWorkbench | None,
    candidates: DetectionCandidateRepository | None,
    associations: SampleAssociationRepository | None,
    corpus_bytes: RuleCorpusBytesPort | None,
    recorder: EvidenceRecorder | None,
    evidence_observations: EvidenceObservationRepository | None = None,
    evidence_payloads: EvidencePayloadReader | None = None,
    corpus_inventory: RuleCorpusInventoryPort | None = None,
    feature_provider: RuleFeatureProvider | None = None,
    max_tool_seconds: int = 30,
) -> None:
    """Register local, optional rule tools without changing the investigation loop."""
    if not 1 <= max_tool_seconds <= 600:
        raise ValueError("max_tool_seconds must be between 1 and 600")
    inventory = corpus_inventory
    if inventory is None and isinstance(corpus_bytes, RuleCorpusInventoryPort):
        inventory = corpus_bytes
    if inventory is None:
        inventory = _EmptyRuleCorpusInventory()
    service = _RuleTools(
        workbench=workbench,
        candidates=candidates,
        associations=associations,
        corpus_bytes=corpus_bytes,
        recorder=recorder,
        evidence_observations=evidence_observations,
        evidence_payloads=evidence_payloads,
        corpus_inventory=inventory,
        feature_provider=feature_provider,
        max_tool_seconds=max_tool_seconds,
    )
    for name, schema in _TOOL_SCHEMAS.items():
        multiplier = 2 if name == "rule.compare" else 1
        cost = max_tool_seconds * multiplier if name != "rule.get" else 0
        registry.register(
            ToolDefinition(
                name=name,
                schema_version="1",
                input_schema=schema,
                effect=ToolEffect.LOCAL_COMPUTE if name != "rule.get" else ToolEffect.LOCAL_READ,
                cost_model={BudgetDimension.TOOL_SECONDS: cost},
                confidentiality=ToolConfidentiality.INTERNAL,
                limitations=(
                    "Règle locale facultative; aucun statut approuvé ou publié n'est produit.",
                    "Les sorties exposent des hashes et résumés bornés, jamais la source YARA "
                    "ni les octets.",
                ),
                capability_key=_TOOL_CAPABILITY_KEYS[name],
            ),
            _RuleToolAdapter(service, name),
        )


class _RuleToolAdapter(ToolAdapter):
    def __init__(self, service: _RuleTools, tool_name: str) -> None:
        self._service = service
        self._tool_name = tool_name

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()
        try:
            output, status, observation_refs = await self._service.execute(
                self._tool_name, arguments, context
            )
            message = ""
        except _ToolUnavailable as exc:
            output = {"reason": str(exc)}
            status = ToolInvocationStatus.UNAVAILABLE
            observation_refs = ()
            message = "Rule workbench capability is unavailable for this operation."
        except (ValueError, TypeError, KeyError) as exc:
            output = {"reason": _safe_error_code(exc)}
            status = ToolInvocationStatus.FAILED
            observation_refs = ()
            message = "Rule tool arguments or rule data failed local validation."
        except _ToolFailed as exc:
            output = {"reason": str(exc)}
            status = ToolInvocationStatus.FAILED
            observation_refs = ()
            message = "Rule workbench could not complete this local operation."
        except Exception:
            output = {"reason": "local_rule_operation_failed"}
            status = ToolInvocationStatus.FAILED
            observation_refs = ()
            message = "Rule workbench could not complete this local operation."
        consumed_seconds = min(
            math.ceil(max(0.0, time.monotonic() - started)),
            self._service.max_tool_seconds * (2 if self._tool_name == "rule.compare" else 1),
        )
        budget = (
            {BudgetDimension.TOOL_SECONDS: consumed_seconds}
            if self._tool_name != "rule.get"
            else {BudgetDimension.TOOL_SECONDS: 0}
        )
        return ToolResult(
            status=status,
            observation_refs=tuple(observation_refs),
            budget_consumed=budget,
            output=output,
            message=message,
        )


class _RuleTools:
    def __init__(
        self,
        *,
        workbench: RuleWorkbench | None,
        candidates: DetectionCandidateRepository | None,
        associations: SampleAssociationRepository | None,
        corpus_bytes: RuleCorpusBytesPort | None,
        recorder: EvidenceRecorder | None,
        evidence_observations: EvidenceObservationRepository | None,
        evidence_payloads: EvidencePayloadReader | None,
        corpus_inventory: RuleCorpusInventoryPort,
        feature_provider: RuleFeatureProvider | None,
        max_tool_seconds: int,
    ) -> None:
        self.workbench = workbench
        self.candidates = candidates
        self.associations = associations
        self.corpus_bytes = corpus_bytes
        self.recorder = recorder
        self.evidence_observations = evidence_observations
        self.evidence_payloads = evidence_payloads
        self.corpus_inventory = corpus_inventory
        self.feature_provider = feature_provider
        self.max_tool_seconds = max_tool_seconds

    async def execute(
        self, name: str, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        if name == "rule.get":
            return await self.get(arguments, context)
        if self.workbench is None:
            raise _ToolUnavailable("workbench_unavailable")
        if name == "rule.propose":
            self._require_write_ports()
            return await self.propose(arguments, context)
        if name == "rule.test":
            self._require_test_ports()
            return await self.test(arguments, context)
        if name == "rule.revise":
            self._require_write_ports()
            return await self.revise(arguments, context)
        if name == "rule.compare":
            self._require_test_ports()
            return await self.compare(arguments, context)
        raise _ToolUnavailable("unknown_rule_tool")

    def _require_test_ports(self) -> None:
        if (
            self.workbench is None
            or self.candidates is None
            or self.associations is None
            or self.corpus_bytes is None
            or self.recorder is None
        ):
            raise _ToolUnavailable("rule_test_dependencies_unavailable")

    def _require_write_ports(self) -> None:
        self._require_test_ports()

    async def propose(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        assert self.workbench is not None and self.candidates is not None
        motifs = _motifs_from_json(arguments.get("motifs", ()))
        feature_refs = tuple(UUID(value) for value in arguments.get("feature_refs", ()))
        if feature_refs:
            if self.feature_provider is None:
                raise _ToolUnavailable("rule_features_unavailable")
            feature_inputs = await self.feature_provider.load_rule_features(
                context.investigation_id, feature_refs
            )
        else:
            feature_inputs = RuleFeatureInputs()
        generated_from_features = self.workbench.propose_from_features(
            feature_inputs.invariants,
            feature_sets=feature_inputs.feature_sets,
            code_feature_sets=feature_inputs.code_feature_sets,
            sample_sha256_by_id=feature_inputs.sample_sha256_by_id,
            p10_motifs=feature_inputs.p10_motifs,
            observation_refs_by_invariant=feature_inputs.observation_refs_by_invariant,
            goodware_features=feature_inputs.goodware_features,
        )
        unique_motifs: dict[tuple[str, str, str, str], RuleMotif] = {}
        for motif in sorted(
            (*generated_from_features, *motifs),
            key=lambda item: (
                item.kind.value,
                item.value.casefold(),
                item.provenance_kind.value,
                item.provenance_id,
            ),
        ):
            unique_motifs.setdefault(
                (
                    motif.kind.value,
                    motif.value.casefold(),
                    motif.provenance_kind.value,
                    motif.provenance_id,
                ),
                motif,
            )
        generated = tuple(unique_motifs.values())
        if not generated:
            raise _ToolFailed("no_usable_motifs")
        candidate_id = UUID(arguments["candidate_id"]) if arguments.get("candidate_id") else uuid4()
        if await self.candidates.list_for_candidate(candidate_id):
            raise _ToolFailed("candidate_id_already_exists")
        corpus = await self._build_corpus(context.investigation_id)
        result = await self._create_version(
            context,
            candidate_id=candidate_id,
            parent=None,
            motifs=generated,
            condition=arguments["condition"],
            meta=cast(Mapping[str, str | int | bool] | None, arguments.get("meta")),
            corpus=corpus,
            operation="propose",
        )
        output = self._version_output(result)
        return output, result.status, (result.observation.id,)

    async def revise(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        assert self.workbench is not None and self.candidates is not None
        candidate_id = UUID(arguments["candidate_id"])
        versions = await self._versions_for_investigation(candidate_id, context.investigation_id)
        parent_number = arguments["parent_version"]
        parent = next((item for item in versions if item.version == parent_number), None)
        if parent is None:
            raise _ToolFailed("parent_version_not_found")
        if not versions or parent.id != versions[-1].id:
            raise _ToolFailed("parent_must_be_latest")
        if parent.status is DetectionCandidateStatus.APPROVED:
            raise _ToolFailed("approved_versions_are_immutable")
        motifs = _motifs_from_json(arguments["motifs"]) if "motifs" in arguments else None
        condition = arguments.get("condition")
        if motifs is None and condition is None:
            raise _ToolFailed("revision_requires_a_change")
        if motifs is None:
            motifs = _motifs_from_version(parent)
        elif any(not motif.observation_refs for motif in motifs):
            raise _ToolFailed("motifs_require_observation_refs")
        if condition is None:
            condition = _condition_from_source(parent.source)
        meta = arguments.get("meta")
        if meta is None:
            meta = _meta_from_source(parent.source)
        corpus = await self._build_corpus(context.investigation_id)
        result = await self._create_version(
            context,
            candidate_id=candidate_id,
            parent=parent,
            motifs=motifs,
            condition=condition,
            meta=cast(Mapping[str, str | int | bool] | None, meta),
            corpus=corpus,
            operation="revise",
        )
        output = self._version_output(result)
        return output, result.status, (result.observation.id,)

    async def test(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        assert self.workbench is not None and self.candidates is not None
        candidate_id = UUID(arguments["candidate_id"])
        versions = await self._versions_for_investigation(candidate_id, context.investigation_id)
        selected = self._select_version(versions, arguments.get("version"))
        corpus = await self._build_corpus(context.investigation_id)
        report = await self.workbench.compile_and_test(
            selected.source,
            corpus.cases,
            selection_bias=corpus.selection_bias,
            coverage_statement=corpus.coverage_statement,
        )
        evaluation = _status_for_report(report, corpus)
        observation = await self._record_report(
            context,
            report,
            corpus,
            candidate_id=selected.candidate_id,
            version=selected,
            candidate_status=_candidate_status(report, corpus),
            operation="test",
        )
        return (
            {
                "candidate_id": str(selected.candidate_id),
                "version_id": str(selected.id),
                "version": selected.version_label,
                "source_sha256": selected.source_sha256,
                "candidate_status": _candidate_status(report, corpus).value,
                "report": _report_summary(report, observation.id, corpus),
            },
            evaluation,
            (observation.id,),
        )

    async def compare(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        assert self.workbench is not None
        candidate_id = UUID(arguments["candidate_id"])
        versions = await self._versions_for_investigation(candidate_id, context.investigation_id)
        version_a = self._select_version(versions, arguments["version_a"])
        version_b = self._select_version(versions, arguments["version_b"])
        if version_a.id == version_b.id:
            raise _ToolFailed("comparison_requires_two_versions")
        corpus = await self._build_corpus(context.investigation_id)
        comparison = await self.workbench.compare(
            version_a,
            version_b,
            corpus.cases,
            selection_bias=corpus.selection_bias,
            coverage_statement=corpus.coverage_statement,
        )
        observation = await self._record_comparison(
            context, comparison, corpus, version_a, version_b
        )
        output = _comparison_summary(comparison, observation.id, corpus)
        output["finding_candidate"] = {
            "kind": "detection",
            "assessment": "qualified",
            "summary": (
                f"{version_b.version_label} changes matches relative to "
                f"{version_a.version_label}; review corpus coverage before citing it."
            ),
            "observation_refs": [str(observation.id)],
        }
        return output, _status_for_comparison(comparison, corpus), (observation.id,)

    async def get(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        if (
            self.candidates is None
            or self.evidence_observations is None
            or self.evidence_payloads is None
        ):
            raise _ToolUnavailable("rule_history_unavailable")
        candidate_id = UUID(arguments["candidate_id"])
        versions = await self._versions_for_investigation(candidate_id, context.investigation_id)
        if not versions:
            return (
                {"candidate_id": str(candidate_id), "versions": []},
                ToolInvocationStatus.EMPTY,
                (),
            )
        selected_version = arguments.get("version")
        selected = (
            self._select_version(versions, selected_version)
            if selected_version is not None
            else versions[-1]
        )
        include_history = bool(arguments.get("include_history", False))
        visible = versions[-_MAX_HISTORY:] if include_history else (selected,)
        visible_ids = {str(version.id) for version in visible}
        observations = await self.evidence_observations.list_for_investigation(
            context.investigation_id
        )
        report_observations = [
            observation
            for observation in observations
            if observation.kind is EvidenceKind.YARA_MATCH
            and observation.object_type == "detection_candidate_version"
            and observation.object_id in visible_ids
        ]
        reports: dict[str, list[Mapping[str, Any]]] = {version_id: [] for version_id in visible_ids}
        for observation in report_observations:
            try:
                payload = await self.evidence_payloads.read_json(observation.blob_id)
            except Exception:
                continue
            reports[observation.object_id].append(payload)
        comparison_observations = [
            observation
            for observation in observations
            if observation.kind is EvidenceKind.YARA_MATCH
            and observation.object_type == "detection_candidate_comparison"
            and observation.object_id.startswith(f"{candidate_id}:")
        ]
        comparisons: list[Mapping[str, Any]] = []
        for observation in comparison_observations[-_MAX_HISTORY:]:
            try:
                comparisons.append(await self.evidence_payloads.read_json(observation.blob_id))
            except Exception:
                continue
        history = [
            _version_summary(version, reports.get(str(version.id), ())) for version in visible
        ]
        return (
            {
                "candidate_id": str(candidate_id),
                "selected_version": selected.version_label,
                "versions": history,
                "comparisons": comparisons,
            },
            ToolInvocationStatus.SUCCEEDED,
            tuple(observation.id for observation in report_observations),
        )

    async def _create_version(
        self,
        context: ToolExecutionContext,
        *,
        candidate_id: UUID,
        parent: DetectionCandidateVersion | None,
        motifs: Sequence[RuleMotif],
        condition: str,
        meta: Mapping[str, str | int | bool] | None,
        corpus: _RuleCorpusSnapshot,
        operation: str,
    ) -> _CreatedRuleVersion:
        assert self.workbench is not None and self.candidates is not None
        validation = self.workbench.validate_motifs(motifs, meta=meta)
        if not validation.valid:
            raise _ToolFailed("motif_validation_failed")
        if any(not motif.observation_refs for motif in motifs):
            raise _ToolFailed("motifs_require_observation_refs")
        build = self.workbench.build_rule(motifs, condition, meta)
        if not build.valid or build.source is None:
            raise _ToolFailed("rule_build_failed")
        report = await self.workbench.compile_and_test(
            build.source,
            corpus.cases,
            selection_bias=corpus.selection_bias,
            coverage_statement=corpus.coverage_statement,
        )
        status = _candidate_status(report, corpus)
        measurable = sum(len(role.matches) + len(role.non_matches) for role in report.roles)
        evaluation = _status_for_report(report, corpus)
        if evaluation is ToolInvocationStatus.UNAVAILABLE:
            observation = await self._record_report(
                context,
                report,
                corpus,
                candidate_id=candidate_id,
                version=None,
                candidate_status=status,
                operation=operation,
                proposal_sha256=report.source_sha256,
            )
            return _CreatedRuleVersion(
                candidate_id, None, report, observation, evaluation, status, corpus
            )
        if report.execution_status.value not in {"ok", "compile_error"}:
            observation = await self._record_report(
                context,
                report,
                corpus,
                candidate_id=candidate_id,
                version=None,
                candidate_status=status,
                operation=operation,
                proposal_sha256=report.source_sha256,
            )
            return _CreatedRuleVersion(
                candidate_id,
                None,
                report,
                observation,
                ToolInvocationStatus.UNAVAILABLE,
                status,
                corpus,
            )
        if measurable == 0 and report.execution_status.value == "ok":
            observation = await self._record_report(
                context,
                report,
                corpus,
                candidate_id=candidate_id,
                version=None,
                candidate_status=status,
                operation=operation,
                proposal_sha256=report.source_sha256,
            )
            return _CreatedRuleVersion(
                candidate_id,
                None,
                report,
                observation,
                ToolInvocationStatus.UNAVAILABLE,
                status,
                corpus,
            )
        version_number = 1 if parent is None else parent.version + 1
        parent_id = parent.id if parent is not None else None
        version = DetectionCandidateVersion(
            investigation_id=context.investigation_id,
            candidate_id=candidate_id,
            source=build.source,
            source_sha256=report.source_sha256,
            motif_provenance=tuple(
                MotifProvenance(
                    motif=motif.value,
                    rationale=_serialize_motif(motif),
                    observation_refs=motif.observation_refs,
                )
                for motif in sorted(
                    motifs,
                    key=lambda item: (item.kind.value, item.value.casefold(), item.provenance_id),
                )
            ),
            status=status,
            version=version_number,
            parent_version_id=parent_id,
            test_refs=tuple(dict.fromkeys(case.id for case in corpus.cases)),
        )
        stored = await self.candidates.append_version(version, expected_previous_id=parent_id)
        observation = await self._record_report(
            context,
            report,
            corpus,
            candidate_id=candidate_id,
            version=stored,
            candidate_status=status,
            operation=operation,
        )
        return _CreatedRuleVersion(
            candidate_id, stored, report, observation, evaluation, status, corpus
        )

    def _version_output(
        self,
        result: _CreatedRuleVersion,
    ) -> Mapping[str, Any]:
        version = result.version
        output: dict[str, Any] = {
            "candidate_id": str(result.candidate_id),
            "source_sha256": result.report.source_sha256,
            "candidate_status": result.candidate_status.value,
            "execution_status": result.report.execution_status.value,
            "evaluation_status": result.status.value,
            "report": _report_summary(result.report, result.observation.id, result.corpus),
            "observation_refs": [str(result.observation.id)],
        }
        if version is not None:
            output.update(
                {
                    "version_id": str(version.id),
                    "version": version.version_label,
                    "parent_version_id": (
                        str(version.parent_version_id) if version.parent_version_id else None
                    ),
                }
            )
        return output

    async def _build_corpus(self, investigation_id: UUID) -> _RuleCorpusSnapshot:
        assert self.associations is not None
        associations = await self.associations.list_for_investigation(investigation_id)
        by_hash: dict[str, list[SampleAssociation]] = {}
        for association in associations:
            by_hash.setdefault(association.sample_sha256, []).append(association)
        cases: dict[tuple[RuleTestCorpusRole, str], RuleTestCase] = {}
        for sample_sha256, matches in by_hash.items():
            confirmed = any(item.qualification is SampleQualification.CONFIRMED for item in matches)
            documented_seed = any(
                item.origin is SampleOrigin.SEED
                and item.qualification is not SampleQualification.REJECTED
                for item in matches
            )
            role = (
                RuleTestCorpusRole.CONFIRMED_POSITIVE
                if confirmed or documented_seed
                else RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE
            )
            cases[(role, sample_sha256)] = _rule_case(investigation_id, role, sample_sha256)

        limitations: list[str] = []
        try:
            goodware_hashes = await self.corpus_inventory.goodware_sample_sha256s(investigation_id)
        except Exception:
            goodware_hashes = ()
            limitations.append("L'index goodware associé à l'enquête n'a pas pu être lu.")
        if not goodware_hashes:
            limitations.append("Aucun échantillon du baseline goodware associé n'a été fourni.")
        for sample_sha256 in _unique_valid_hashes(goodware_hashes):
            cases[(RuleTestCorpusRole.GOODWARE, sample_sha256)] = _rule_case(
                investigation_id, RuleTestCorpusRole.GOODWARE, sample_sha256
            )

        try:
            holdout_hashes = await self.corpus_inventory.holdout_sample_sha256s(investigation_id)
        except Exception:
            holdout_hashes = ()
            limitations.append("Le holdout indépendant n'a pas pu être lu.")
        for sample_sha256 in _unique_valid_hashes(holdout_hashes):
            cases[(RuleTestCorpusRole.HOLDOUT, sample_sha256)] = _rule_case(
                investigation_id, RuleTestCorpusRole.HOLDOUT, sample_sha256
            )
        ordered_cases = tuple(
            cases[key] for key in sorted(cases, key=lambda item: (item[0].value, item[1]))
        )
        coverage = (
            "Corpus exact par SHA-256; positifs confirmés et seeds documentés, "
            "candidats hors-cluster, "
            "sources du baseline goodware et holdout indépendant lorsque disponibles. Les octets "
            "absents de la quarantaine ou du baseline restent non mesurables."
        )
        selection_bias = (
            "Corpus de convenance propre à cette enquête: les seeds et associations reflètent "
            "la sélection antérieure; les résultats ne mesurent pas une prévalence universelle."
        )
        return _RuleCorpusSnapshot(
            cases=ordered_cases,
            selection_bias=selection_bias,
            coverage_statement=coverage,
            limitations=tuple(dict.fromkeys(limitations)),
        )

    async def _versions_for_investigation(
        self, candidate_id: UUID, investigation_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]:
        if self.candidates is None:
            raise _ToolUnavailable("detection_candidates_unavailable")
        versions = tuple(
            item
            for item in await self.candidates.list_for_candidate(candidate_id)
            if item.investigation_id == investigation_id
        )
        return tuple(sorted(versions, key=lambda item: item.version))

    @staticmethod
    def _select_version(
        versions: Sequence[DetectionCandidateVersion], version_number: int | None
    ) -> DetectionCandidateVersion:
        if not versions:
            raise _ToolFailed("candidate_not_found")
        if version_number is None:
            return versions[-1]
        for version in versions:
            if version.version == version_number:
                return version
        raise _ToolFailed("candidate_version_not_found")

    async def _record_report(
        self,
        context: ToolExecutionContext,
        report: RuleTestReport,
        corpus: _RuleCorpusSnapshot,
        *,
        candidate_id: UUID,
        version: DetectionCandidateVersion | None,
        candidate_status: DetectionCandidateStatus,
        operation: str,
        proposal_sha256: str | None = None,
    ) -> EvidenceObservation:
        if self.recorder is None:
            raise _ToolUnavailable("evidence_recorder_unavailable")
        version_ref = str(version.id) if version is not None else f"pending:{report.source_sha256}"
        payload = _report_payload(report, corpus, candidate_status)
        report_fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        observation = await self.recorder.record_json(
            investigation_id=context.investigation_id,
            kind=EvidenceKind.YARA_MATCH,
            payload={
                "schema": "investigation-yara-test.v1",
                "operation": operation,
                "candidate_id": str(candidate_id),
                "version_id": version_ref,
                "proposal_source_sha256": proposal_sha256 or report.source_sha256,
                **payload,
            },
            source="AutoWork RuleWorkbench",
            source_version="investigation-rule-tools.v1",
            object_type="detection_candidate_version",
            object_id=version_ref,
            limitations=corpus.limitations,
            idempotency_key=(
                f"{context.idempotency_key}:rule:{operation}:"
                f"{candidate_id}:{version_ref}:{report_fingerprint}"
            ),
        )
        return observation

    async def _record_comparison(
        self,
        context: ToolExecutionContext,
        comparison: RuleVersionComparison,
        corpus: _RuleCorpusSnapshot,
        version_a: DetectionCandidateVersion,
        version_b: DetectionCandidateVersion,
    ) -> EvidenceObservation:
        if self.recorder is None:
            raise _ToolUnavailable("evidence_recorder_unavailable")
        object_id = f"{version_a.candidate_id}:{version_a.version}:{version_b.version}"
        roles = corpus.hashes_by_role()
        comparison_payload = {
            "schema": "investigation-yara-comparison.v1",
            "candidate_id": str(version_a.candidate_id),
            "version_a_id": str(version_a.id),
            "version_b_id": str(version_b.id),
            "source_sha256_a": version_a.source_sha256,
            "source_sha256_b": version_b.source_sha256,
            "engine_version_a": comparison.engine_version_a,
            "engine_version_b": comparison.engine_version_b,
            "corpus_sha256s_by_role": {role.value: list(hashes) for role, hashes in roles.items()},
            "roles": [
                {
                    "role": item.role.value,
                    "matches_gained": list(item.matches_gained),
                    "matches_lost": list(item.matches_lost),
                    "non_comparable": list(item.non_comparable),
                }
                for item in comparison.roles
            ],
            "false_positives_added": list(comparison.false_positives_added),
            "false_positives_removed": list(comparison.false_positives_removed),
            "limitations": list(corpus.limitations),
        }
        comparison_fingerprint = hashlib.sha256(
            json.dumps(comparison_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        observation = await self.recorder.record_json(
            investigation_id=context.investigation_id,
            kind=EvidenceKind.YARA_MATCH,
            payload=comparison_payload,
            source="AutoWork RuleWorkbench",
            source_version="investigation-rule-tools.v1",
            object_type="detection_candidate_comparison",
            object_id=object_id,
            limitations=corpus.limitations,
            idempotency_key=(
                f"{context.idempotency_key}:rule:compare:{object_id}:{comparison_fingerprint}"
            ),
        )
        return observation


def _candidate_status(
    report: RuleTestReport, corpus: _RuleCorpusSnapshot
) -> DetectionCandidateStatus:
    if report.execution_status.value == "compile_error":
        return DetectionCandidateStatus.REJECTED
    positive = _role_for(report, RuleTestCorpusRole.CONFIRMED_POSITIVE)
    goodware = _role_for(report, RuleTestCorpusRole.GOODWARE)
    if goodware.matches:
        return DetectionCandidateStatus.REJECTED
    if report.execution_status.value != "ok":
        return DetectionCandidateStatus.CANDIDATE
    if len(positive.matches) == 1 and not positive.non_matches and not positive.non_measurable:
        return DetectionCandidateStatus.EXPERIMENTAL
    if (
        len(positive.matches) > 1
        and not positive.non_matches
        and not positive.non_measurable
        and not positive.errors
        and not any(
            role.matches or role.non_measurable or role.errors
            for role in report.roles
            if role.role is not RuleTestCorpusRole.CONFIRMED_POSITIVE
        )
        and not corpus.limitations
    ):
        return DetectionCandidateStatus.TESTED
    return DetectionCandidateStatus.CANDIDATE


def _status_for_report(report: RuleTestReport, corpus: _RuleCorpusSnapshot) -> ToolInvocationStatus:
    if report.execution_status.value == "compile_error":
        return ToolInvocationStatus.SUCCEEDED
    if report.execution_status.value != "ok":
        return ToolInvocationStatus.UNAVAILABLE
    measurable = sum(len(role.matches) + len(role.non_matches) for role in report.roles)
    if not corpus.cases or measurable == 0:
        return ToolInvocationStatus.UNAVAILABLE
    if corpus.limitations or any(role.non_measurable or role.errors for role in report.roles):
        return ToolInvocationStatus.PARTIAL
    return ToolInvocationStatus.SUCCEEDED


def _status_for_comparison(
    comparison: RuleVersionComparison, corpus: _RuleCorpusSnapshot
) -> ToolInvocationStatus:
    has_measurable_delta = any(
        item.matches_gained or item.matches_lost for item in comparison.roles
    )
    if not corpus.cases:
        return ToolInvocationStatus.UNAVAILABLE
    if corpus.limitations or any(item.non_comparable for item in comparison.roles):
        return ToolInvocationStatus.PARTIAL
    return (
        ToolInvocationStatus.SUCCEEDED
        if has_measurable_delta or corpus.cases
        else ToolInvocationStatus.EMPTY
    )


def _report_payload(
    report: RuleTestReport,
    corpus: _RuleCorpusSnapshot,
    candidate_status: DetectionCandidateStatus,
) -> dict[str, Any]:
    return {
        "source_sha256": report.source_sha256,
        "execution_status": report.execution_status.value,
        "engine_version": report.engine_version[:200],
        "candidate_status": candidate_status.value,
        "selection_bias": corpus.selection_bias,
        "coverage_statement": corpus.coverage_statement,
        "corpus_sha256": report.corpus_sha256,
        "corpus_sha256s_by_role": {
            role.value: list(hashes) for role, hashes in corpus.hashes_by_role().items()
        },
        "roles": [
            {
                "role": role.role.value,
                "corpus_sha256s": list(role.corpus_sha256s),
                "matches": list(role.matches),
                "non_matches": list(role.non_matches),
                "non_measurable": list(role.non_measurable),
                "errors": [
                    {
                        "code": error.code.value,
                        "sample_sha256": error.sample_sha256,
                        "line": error.line,
                    }
                    for error in role.errors
                ],
            }
            for role in report.roles
        ],
        "limitations": list(corpus.limitations),
    }


def _report_summary(
    report: RuleTestReport,
    observation_id: UUID | None,
    corpus: _RuleCorpusSnapshot | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_sha256": report.source_sha256,
        "execution_status": report.execution_status.value,
        "engine_version": report.engine_version[:200],
        "corpus_sha256": report.corpus_sha256,
        "roles": [
            {
                "role": role.role.value,
                "corpus_sha256s": list(role.corpus_sha256s),
                "matches": list(role.matches),
                "non_matches": list(role.non_matches),
                "non_measurable": list(role.non_measurable),
                "error_count": len(role.errors),
            }
            for role in report.roles
        ],
    }
    if observation_id is not None:
        result["observation_ref"] = str(observation_id)
    if corpus is not None:
        result["limitations"] = list(corpus.limitations)
    return result


def _comparison_summary(
    comparison: RuleVersionComparison,
    observation_id: UUID,
    corpus: _RuleCorpusSnapshot,
) -> dict[str, Any]:
    return {
        "version_a_id": str(comparison.version_a_id),
        "version_b_id": str(comparison.version_b_id),
        "version_a": comparison.version_a,
        "version_b": comparison.version_b,
        "engine_version_a": comparison.engine_version_a,
        "engine_version_b": comparison.engine_version_b,
        "corpus_sha256s_by_role": {
            role.value: list(hashes) for role, hashes in corpus.hashes_by_role().items()
        },
        "roles": [
            {
                "role": role.role.value,
                "matches_gained": list(role.matches_gained),
                "matches_lost": list(role.matches_lost),
                "non_comparable": list(role.non_comparable),
            }
            for role in comparison.roles
        ],
        "false_positives_added": list(comparison.false_positives_added),
        "false_positives_removed": list(comparison.false_positives_removed),
        "limitations": list(corpus.limitations),
        "observation_ref": str(observation_id),
    }


def _version_summary(
    version: DetectionCandidateVersion, reports: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "version_id": str(version.id),
        "version": version.version_label,
        "parent_version_id": str(version.parent_version_id) if version.parent_version_id else None,
        "source_sha256": version.source_sha256,
        "status": version.status.value,
        "test_refs": [str(reference) for reference in version.test_refs],
        "motif_count": len(version.motif_provenance),
        "motif_kinds": sorted(
            {_motif_kind_from_provenance(item.rationale) for item in version.motif_provenance}
        ),
        "reports": list(reports),
    }


def _motifs_from_json(raw_motifs: Sequence[Mapping[str, Any]]) -> tuple[RuleMotif, ...]:
    if not isinstance(raw_motifs, Sequence) or isinstance(raw_motifs, (str, bytes)):
        raise ValueError("motifs must be an array")
    result: list[RuleMotif] = []
    for item in raw_motifs:
        if not isinstance(item, Mapping):
            raise ValueError("each motif must be an object")
        values = dict(item)
        values["kind"] = RuleMotifKind(values["kind"])
        values["provenance_kind"] = RuleMotifProvenanceKind(values["provenance_kind"])
        values["observation_refs"] = tuple(UUID(value) for value in values["observation_refs"])
        if "offsets" in values:
            values["offsets"] = tuple(values["offsets"])
        result.append(RuleMotif(**values))
    return tuple(result)


def _motifs_from_version(version: DetectionCandidateVersion) -> tuple[RuleMotif, ...]:
    motifs: list[RuleMotif] = []
    for item in version.motif_provenance:
        if not item.rationale.startswith(_PERSISTED_MOTIF_PREFIX):
            raise _ToolFailed("parent_motif_provenance_unreadable")
        try:
            metadata = json.loads(item.rationale[len(_PERSISTED_MOTIF_PREFIX) :])
            motifs.append(
                RuleMotif(
                    kind=RuleMotifKind(metadata["kind"]),
                    value=item.motif,
                    provenance_kind=RuleMotifProvenanceKind(metadata["provenance_kind"]),
                    provenance_id=metadata["provenance_id"],
                    observation_refs=item.observation_refs,
                    sample_sha256=metadata.get("sample_sha256"),
                    offsets=(
                        tuple(metadata["offsets"]) if metadata.get("offsets") is not None else None
                    ),
                    rationale=metadata.get("rationale", ""),
                    positive_support=metadata.get("positive_support"),
                    benign_prevalence=metadata.get("benign_prevalence"),
                    goodware_occurrence_count=metadata.get("goodware_occurrence_count"),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _ToolFailed("parent_motif_provenance_unreadable") from exc
    return tuple(motifs)


def _condition_from_source(source: str) -> RuleConditionStrategy:
    match = re.search(
        r'^\s*workbench_condition\s*=\s*"(any|all|majority)"\s*$', source, re.MULTILINE
    )
    if match is None:
        raise _ToolFailed("parent_condition_unreadable")
    return RuleConditionStrategy(match.group(1))


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


def _motif_kind_from_provenance(rationale: str) -> str:
    if not rationale.startswith(_PERSISTED_MOTIF_PREFIX):
        return "unknown"
    try:
        payload = json.loads(rationale[len(_PERSISTED_MOTIF_PREFIX) :])
    except (TypeError, ValueError, json.JSONDecodeError):
        return "unknown"
    return str(payload.get("kind", "unknown"))


def _meta_from_source(source: str) -> dict[str, str | int | bool]:
    meta: dict[str, str | int | bool] = {}
    in_meta = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped == "meta:":
            in_meta = True
            continue
        if stripped in {"strings:", "condition:"}:
            in_meta = False
        if not in_meta or "=" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split("=", maxsplit=1))
        if key in {"generated_by", "workbench_condition"} or key.startswith("motif_"):
            continue
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            continue
        if isinstance(parsed, (str, int, bool)):
            meta[key] = parsed
    return meta


def _role_for(report: RuleTestReport, role: RuleTestCorpusRole) -> RuleRoleTestReport:
    return next(item for item in report.roles if item.role is role)


def _rule_case(
    investigation_id: UUID, role: RuleTestCorpusRole, sample_sha256: str
) -> RuleTestCase:
    case_id = uuid5(_UUID_NAMESPACE, f"{investigation_id}:{role.value}:{sample_sha256}")
    return RuleTestCase(id=case_id, role=role, sample_sha256=sample_sha256)


def _unique_valid_hashes(values: Sequence[str]) -> tuple[str, ...]:
    hashes = set()
    for value in values:
        if isinstance(value, str) and re.fullmatch(_SHA256_PATTERN, value):
            hashes.add(value)
    return tuple(sorted(hashes))


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return "missing_argument"
    if isinstance(exc, TypeError):
        return "invalid_argument_type"
    if isinstance(exc, ValueError):
        return "invalid_rule_input"
    return "local_rule_operation_failed"


__all__ = [
    "JSON_SCHEMA_2020_12",
    "RULE_COMPARE_SCHEMA",
    "RULE_GET_SCHEMA",
    "RULE_PROPOSE_SCHEMA",
    "RULE_REVISE_SCHEMA",
    "RULE_TEST_SCHEMA",
    "RULE_WORKBENCH_CAPABILITY",
    "EvidencePayloadReader",
    "QuarantineRuleCorpusBytes",
    "RuleCorpusInventoryPort",
    "RuleFeatureInputs",
    "RuleFeatureProvider",
    "register_rule_tools",
    "rule_tool_capabilities",
]

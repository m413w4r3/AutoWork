"""Optional, local YARA tools exposed to the investigation tool registry."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from difflib import unified_diff
from typing import Any, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4, uuid5

from cti_app.application.advisor_contracts import ADVISOR_SOURCE_PARSER_VERSION
from cti_app.application.blob_storage import (
    BlobReadLimitExceededError,
    BlobStorageUnavailableError,
    BlobStore,
)
from cti_app.application.confined_analysis import confined_analysis_operation_cache_identity
from cti_app.application.evidence_recorder import EvidenceRecorder, sanitize_evidence_text
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
    validate_json_schema,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.rule_corpus_import import list_manual_rule_corpus_imports
from cti_app.application.rule_workbench import (
    RuleCorpusBytesPort,
    RuleCorpusBytesUnavailable,
    RuleValidationReason,
    RuleWorkbench,
    is_generic_dotnet_import,
    rule_validation_reason,
    validate_single_rule_candidate,
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
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.rule_workbench import (
    OneRuleTestStatus,
    RuleConditionStrategy,
    RuleCorpusInventoryItem,
    RuleMotif,
    RuleMotifKind,
    RuleMotifProvenanceKind,
    RuleRoleTestReport,
    RuleTestCase,
    RuleTestCorpusRole,
    RuleTestReport,
    RuleValidationStatus,
    RuleVersionComparison,
    YaraEngineResult,
    YaraExecutionStatus,
    rule_corpus_sha256,
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
MAX_RULE_REPORT_SUMMARY_BYTES = 64 * 1024
_HOLDOUT_ROLES_FOR_TEST = frozenset(
    {RuleTestCorpusRole.HOLDOUT_POSITIVE, RuleTestCorpusRole.HOLDOUT_NEGATIVE}
)


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


def _allow_empty_optional(schema: Mapping[str, Any], description: str) -> dict[str, Any]:
    result = dict(schema)
    schema_type = result["type"]
    if schema_type == "string":
        pattern = result.get("pattern")
        result["pattern"] = f"(?:{pattern})|^$" if pattern else "^$"
    else:
        result["type"] = [schema_type, "string"]
        result["pattern"] = "^$"
    result["description"] = description
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
        "article_claim_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
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
    "required": ["kind", "value", "provenance_kind", "provenance_id"],
}

_META_OBJECT_SCHEMA: dict[str, Any] = {
    "type": ["object", "string", "null"],
    "additionalProperties": False,
    "properties": {
        "classification": {
            "type": "string",
            "maxLength": 128,
            "pattern": _SAFE_TEXT_PATTERN,
        },
        "family": {"type": "string", "maxLength": 128, "pattern": _SAFE_TEXT_PATTERN},
        "description": {"type": "string", "maxLength": 512, "pattern": _SAFE_TEXT_PATTERN},
        "hypothesis": {"type": "string", "maxLength": 512, "pattern": _SAFE_TEXT_PATTERN},
        "dotnet_imports_discussion": {
            "type": "string",
            "maxLength": 512,
            "pattern": _SAFE_TEXT_PATTERN,
        },
        "source": {"type": "string", "maxLength": 128, "pattern": _SAFE_TEXT_PATTERN},
    },
}
_META_INPUT_SCHEMA: dict[str, Any] = {
    **_META_OBJECT_SCHEMA,
    "default": {},
    "description": (
        "Optional JSON object with classification, family, description, or source. Omit it, "
        "use {}, or leave it empty for no metadata. Example: ARG meta: "
        '{"description": "..."}. A JSON-encoded object string is also accepted.'
    ),
}
_META_AGENT_ERROR = (
    'meta must be an object or a JSON-encoded object. Example: ARG meta: {"description": "..."}'
)

RULE_PROPOSE_SCHEMA = _schema(
    {
        "motifs": _allow_empty_optional(
            {"type": "array", "items": _MOTIF_SCHEMA, "maxItems": 128},
            "Optional structured motif array; an empty value means no supplied motifs.",
        ),
        "feature_refs": _allow_empty_optional(
            {
                "type": "array",
                "items": {"type": "string", "pattern": _UUID_PATTERN},
                "maxItems": 64,
                "uniqueItems": True,
            },
            "Optional feature UUID array; an empty value means no feature references.",
        ),
        "candidate_id": _allow_empty_optional(
            {"type": "string", "pattern": _UUID_PATTERN},
            "Optional UUID; omit or leave empty to create a new candidate ID.",
        ),
        "condition": {
            "type": "string",
            "enum": [item.value for item in RuleConditionStrategy],
            "description": "Required strategy: all, any, or majority; this is not free text.",
        },
        "meta": _META_INPUT_SCHEMA,
    },
    required=("condition",),
)

RULE_PROPOSE_BRIDGE_SCHEMA = _schema(
    {
        "bridge_observation_id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": (
                "Evidence observation returned by a successful advisor.consult yara_draft v3 call."
            ),
        },
        "bridge_invocation_id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": "Bridge invocation id persisted on that observation.",
        },
        "rule_source_sha256": {
            "type": "string",
            "pattern": _SHA256_PATTERN,
            "description": "SHA-256 from the complete persisted yara_draft rule_source.",
        },
        "candidate_id": _allow_empty_optional(
            {"type": "string", "pattern": _UUID_PATTERN},
            "Optional UUID; omit or leave empty to create a new candidate.",
        ),
    },
    ("bridge_observation_id", "bridge_invocation_id", "rule_source_sha256"),
)

RULE_OPERATOR_SYNTAX_FIX_SCHEMA = _schema(
    {
        "candidate_id": {"type": "string", "pattern": _UUID_PATTERN},
        "parent_version": {"type": "integer", "minimum": 1},
        "source": {
            "type": "string",
            "minLength": 1,
            "pattern": r"^(?!.*\x00)[\s\S]*$",
        },
        "author": {"type": "string", "minLength": 1, "maxLength": 256},
    },
    ("candidate_id", "parent_version", "source", "author"),
)

RULE_TEST_SCHEMA = _schema(
    {
        "candidate_id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": (
                "Use candidate_id returned by rule.propose or listed in investigation.rule_deltas "
                "under kind=rule_candidate."
            ),
        },
        "version": _allow_empty_optional(
            {"type": "integer", "minimum": 1},
            "Optional version number; omit or leave empty to select the latest version.",
        ),
    },
    ("candidate_id",),
)

RULE_REVISE_SCHEMA = _schema(
    {
        "candidate_id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": (
                "Use candidate_id returned by rule.propose or listed in investigation.rule_deltas "
                "under kind=rule_candidate."
            ),
        },
        "parent_version": {
            "type": "integer",
            "minimum": 1,
            "description": "Use the current version shown for that candidate in rule_deltas.",
        },
        "motifs": _allow_empty_optional(
            {"type": "array", "items": _MOTIF_SCHEMA, "maxItems": 128},
            "Optional structured motif array; empty means retain the current motifs.",
        ),
        "condition": {
            "type": "string",
            "enum": [*(item.value for item in RuleConditionStrategy), ""],
            "description": (
                "Optional strategy: all, any, or majority. Omit or leave empty to retain it."
            ),
        },
        "meta": _META_INPUT_SCHEMA,
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
        "version": _allow_empty_optional(
            {"type": "integer", "minimum": 1},
            "Optional version number; omit or leave empty to select the latest version.",
        ),
        "include_history": _allow_empty_optional(
            {"type": "boolean"},
            "Optional boolean; omit or leave empty to return only the selected version.",
        ),
    },
    ("candidate_id",),
)

_TOOL_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "rule.propose": RULE_PROPOSE_SCHEMA,
    "rule.propose_bridge": RULE_PROPOSE_BRIDGE_SCHEMA,
    "rule.operator_syntax_fix": RULE_OPERATOR_SYNTAX_FIX_SCHEMA,
    "rule.test": RULE_TEST_SCHEMA,
    "rule.revise": RULE_REVISE_SCHEMA,
    "rule.compare": RULE_COMPARE_SCHEMA,
    "rule.get": RULE_GET_SCHEMA,
}
_TOOL_CAPABILITY_KEYS = {name: name for name in _TOOL_SCHEMAS}


class EvidencePayloadReader(Protocol):
    """Read the canonical JSON payload behind a recorded evidence blob."""

    async def read_json(self, blob_id: UUID) -> dict[str, Any]: ...


class RuleArticleClaimPort(Protocol):
    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...]
    ) -> Sequence[Any]: ...


@runtime_checkable
class RuleCorpusInventoryPort(Protocol):
    async def goodware_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]: ...

    async def holdout_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]: ...

    async def manual_rule_corpus_imports(
        self, investigation_id: UUID
    ) -> Sequence[RuleCorpusInventoryItem]: ...


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

    async def manual_rule_corpus_imports(
        self, investigation_id: UUID
    ) -> Sequence[RuleCorpusInventoryItem]:
        del investigation_id
        return ()


class QuarantineRuleCorpusBytes(RuleCorpusBytesPort):
    """Resolve exact sample bytes from quarantine and bound goodware source blobs."""

    _READABLE_BUCKETS = (
        SAMPLES_QUARANTINE_BUCKET,
        "network-captures-quarantine",
        "goodware-baselines",
    )

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

    async def get_artifact_id_for_sample(self, sample_sha256: str) -> UUID:
        if (
            not isinstance(sample_sha256, str)
            or re.fullmatch(_SHA256_PATTERN, sample_sha256) is None
        ):
            raise RuleCorpusBytesUnavailable("sample SHA-256 is invalid")
        async with self._uow_factory() as uow:
            record = None
            for bucket in (SAMPLES_QUARANTINE_BUCKET, "network-captures-quarantine"):
                record = await uow.blobs.get_by_address(bucket, sample_sha256)
                if record is not None:
                    break
        if record is None:
            raise RuleCorpusBytesUnavailable("sample bytes are not present in quarantine")
        descriptor = record.descriptor
        if (
            descriptor.logical_bucket
            not in {SAMPLES_QUARANTINE_BUCKET, "network-captures-quarantine"}
            or descriptor.sha256 != sample_sha256
            or descriptor.size > self._maximum_sample_bytes
        ):
            raise RuleCorpusBytesUnavailable(
                "sample artifact identity is outside the bounded corpus"
            )
        return record.id

    async def goodware_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        # Goodware baselines contain feature fingerprints, not benign file bytes.
        del investigation_id
        return ()

    async def holdout_sample_sha256s(self, investigation_id: UUID) -> Sequence[str]:
        if self._holdout_sample_provider is None:
            return ()
        return tuple(await self._holdout_sample_provider(investigation_id))

    async def manual_rule_corpus_imports(
        self, investigation_id: UUID
    ) -> Sequence[RuleCorpusInventoryItem]:
        return await list_manual_rule_corpus_imports(self._uow_factory, investigation_id)

    async def goodware_baseline_id(self, investigation_id: UUID) -> UUID | None:
        async with self._uow_factory() as uow:
            baseline_id = await uow.investigation_goodware_baselines.get(investigation_id)
            if baseline_id is None:
                return None
            if await uow.goodware_baselines.get(baseline_id) is None:
                return None
            return baseline_id

    async def rule_candidate_versions(
        self, investigation_id: UUID
    ) -> Sequence[DetectionCandidateVersion]:
        async with self._uow_factory() as uow:
            return await uow.detection_candidates.list_for_investigation(investigation_id)


@dataclass(frozen=True, slots=True)
class _RuleCorpusSnapshot:
    cases: tuple[RuleTestCase, ...]
    selection_bias: str
    coverage_statement: str
    limitations: tuple[str, ...]
    blocking_limitations: tuple[str, ...] = ()
    measurement_limitations: tuple[str, ...] = ()
    holdout_overlap_sha256s: tuple[str, ...] = ()
    conflicting_role_sha256s: tuple[str, ...] = ()
    goodware_baseline_id: UUID | None = None

    def hashes_by_role(self) -> dict[RuleTestCorpusRole, tuple[str, ...]]:
        return {
            role: tuple(sorted({case.sample_sha256 for case in self.cases if case.role is role}))
            for role in RuleTestCorpusRole
        }


@dataclass(frozen=True, slots=True)
class _CreatedRuleCandidate:
    version: DetectionCandidateVersion
    compile_check: YaraEngineResult


class _ToolUnavailable(RuntimeError):
    pass


class _ToolFailed(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        agent_message: str | None = None,
        diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.agent_message = agent_message
        self.diagnostic = dict(diagnostic) if diagnostic is not None else None


class _RuleTestInputFailure(ValueError):
    def __init__(self, stage: str, cause: Exception) -> None:
        reason = _safe_rule_test_diagnostic_reason(str(cause))
        super().__init__(reason)
        self.diagnostic: dict[str, str] = {
            "stage": stage,
            "exception_class": _safe_exception_type_name(cause),
            "reason": reason,
        }
        lowered = str(cause).casefold()
        known_roles = tuple(role.value for role in RuleTestCorpusRole)
        matching_roles = [role for role in known_roles if role in lowered]
        if len(matching_roles) == 1:
            self.diagnostic["role"] = matching_roles[0]
        elif "holdout" in lowered:
            self.diagnostic["role"] = "holdout"


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
            _TOOL_CAPABILITY_KEYS[name]
            for name in (
                "rule.propose_bridge",
                "rule.operator_syntax_fix",
                "rule.test",
                "rule.revise",
                "rule.compare",
            )
        )
        capabilities.add(_TOOL_CAPABILITY_KEYS["rule.propose"])
    if all(port is not None for port in (candidates, evidence_observations, evidence_payloads)):
        capabilities.add(_TOOL_CAPABILITY_KEYS["rule.get"])
    return frozenset(capabilities)


def register_rule_tools(
    registry: ToolRegistry,
    *,
    tool_names: tuple[str, ...] | None = None,
    workbench: RuleWorkbench | None,
    candidates: DetectionCandidateRepository | None,
    associations: SampleAssociationRepository | None,
    corpus_bytes: RuleCorpusBytesPort | None,
    recorder: EvidenceRecorder | None,
    artifact_store: ProductionArtifactStore | None = None,
    evidence_observations: EvidenceObservationRepository | None = None,
    evidence_payloads: EvidencePayloadReader | None = None,
    corpus_inventory: RuleCorpusInventoryPort | None = None,
    feature_provider: RuleFeatureProvider | None = None,
    article_claims: RuleArticleClaimPort | None = None,
    max_tool_seconds: int = 30,
) -> _RuleTools:
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
        artifact_store=artifact_store,
        evidence_observations=evidence_observations,
        evidence_payloads=evidence_payloads,
        corpus_inventory=inventory,
        feature_provider=feature_provider,
        article_claims=article_claims,
        max_tool_seconds=max_tool_seconds,
    )
    selected_names = tuple(_TOOL_SCHEMAS) if tool_names is None else tool_names
    for name in selected_names:
        schema = _TOOL_SCHEMAS[name]
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
                    *(
                        (
                            "rule.propose et rule.revise ne font qu'un contrôle de compilation; "
                            "seul un appel explicite à rule.test produit un statut de validation.",
                        )
                        if name in {"rule.propose", "rule.revise"}
                        else ()
                    ),
                    *(
                        (
                            "rule.test rapporte le corpus et ses résultats mesurables; une "
                            "révision exige un faux négatif mesuré sur un positif confirmé; "
                            "conservez sa provenance réelle ou synthétique.",
                        )
                        if name == "rule.test"
                        else ()
                    ),
                    *(
                        (
                            "meta est facultatif et doit être un objet JSON; omettez-le ou "
                            "utilisez "
                            '{} pour aucune méta. Exemple: ARG meta: {"description": "..."}.',
                        )
                        if name in {"rule.propose", "rule.revise"}
                        else ()
                    ),
                    *(
                        (
                            "Pour tester ou réviser, passez candidate_id de rule.propose ou d'une "
                            "entrée rule_deltas kind=rule_candidate.",
                        )
                        if name in {"rule.test", "rule.revise"}
                        else ()
                    ),
                ),
                capability_key=_TOOL_CAPABILITY_KEYS[name],
            ),
            _RuleToolAdapter(service, name),
        )
    return service


_logger = logging.getLogger(__name__)


class _RuleToolAdapter(ToolAdapter):
    def __init__(self, service: _RuleTools, tool_name: str) -> None:
        self._service = service
        self._tool_name = tool_name

    @property
    def operation_cache_identity(self) -> Mapping[str, Any] | None:
        workbench = self._service.workbench
        execution = getattr(workbench, "_confined_analysis", None)
        if execution is None:
            return None
        return confined_analysis_operation_cache_identity(execution)

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()
        exception_class: str | None = None
        error_code: str | None = None
        try:
            output, status, observation_refs = await self._service.execute(
                self._tool_name, arguments, context
            )
            message = ""
        except _ToolUnavailable as exc:
            error_code = "rule_workbench_unavailable"
            output = {"error_code": error_code, "reason": _bounded_reason(str(exc))}
            status = ToolInvocationStatus.UNAVAILABLE
            observation_refs = ()
            message = (
                f"Rule workbench operation is unavailable ({error_code}). "
                "Check the listed capability and report this code if it remains unavailable."
            )
            exception_class = _bounded_exception_chain(exc)
        except (ValueError, TypeError, KeyError) as exc:
            error_code = _safe_error_code(exc)
            output = {"error_code": error_code, "reason": error_code}
            if isinstance(exc, _RuleTestInputFailure):
                output["diagnostic"] = dict(exc.diagnostic)
            status = ToolInvocationStatus.FAILED
            observation_refs = ()
            message = f"Rule tool input failed local validation ({error_code}). "
            message += (
                "See diagnostic for the failing validation stage and reason."
                if isinstance(exc, _RuleTestInputFailure)
                else "Correct the named argument or motif and retry."
            )
            exception_class = _bounded_exception_chain(exc)
        except _ToolFailed as exc:
            error_code = _bounded_reason(str(exc))
            output = {"error_code": error_code, "reason": error_code}
            if exc.diagnostic is not None:
                output["diagnostic"] = dict(exc.diagnostic)
            status = ToolInvocationStatus.FAILED
            observation_refs = ()
            message = exc.agent_message or (
                f"Rule workbench rejected this operation ({error_code}). "
                "Correct the stated input or precondition, then retry the operation."
            )
            exception_class = _bounded_exception_chain(exc)
            _logger.warning(
                "investigation rule tool operation failed locally",
                extra={
                    "event": "investigation_tool_rule_operation_failure",
                    "tool_name": self._tool_name,
                    "exception_class": exception_class,
                    "error_code": error_code,
                },
            )
        except Exception as exc:
            error_code = "local_rule_operation_failed"
            cause_summary = _unexpected_operation_error_code(exc)
            output = {"error_code": error_code, "reason": cause_summary}
            status = ToolInvocationStatus.FAILED
            observation_refs = ()
            message = (
                f"Rule workbench operation failed locally ({error_code}). "
                "Check the candidate index for a persisted version before retrying; if the failure "
                "persists, report this code."
            )
            exception_class = _bounded_exception_chain(exc)
            _logger.warning(
                "investigation rule tool operation failed unexpectedly",
                extra={
                    "event": "investigation_tool_rule_operation_failure",
                    "tool_name": self._tool_name,
                    "exception_class": exception_class,
                    "error_code": error_code,
                    "cause_summary": cause_summary,
                },
            )
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
            exception_class=exception_class,
            error_code=error_code,
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
        artifact_store: ProductionArtifactStore | None = None,
        evidence_observations: EvidenceObservationRepository | None,
        evidence_payloads: EvidencePayloadReader | None,
        corpus_inventory: RuleCorpusInventoryPort,
        feature_provider: RuleFeatureProvider | None,
        article_claims: RuleArticleClaimPort | None = None,
        max_tool_seconds: int = 30,
    ) -> None:
        self.workbench = workbench
        self.candidates = candidates
        self.associations = associations
        self.corpus_bytes = corpus_bytes
        self.recorder = recorder
        self.artifact_store = artifact_store
        self.evidence_observations = evidence_observations
        self.evidence_payloads = evidence_payloads
        self.corpus_inventory = corpus_inventory
        self.feature_provider = feature_provider
        self.article_claims = article_claims
        self.max_tool_seconds = max_tool_seconds

    @property
    def signature_bridge_available(self) -> bool:
        return all(
            port is not None
            for port in (
                self.workbench,
                self.candidates,
                self.associations,
                self.corpus_bytes,
                self.recorder,
            )
        )

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
        if name == "rule.propose_bridge":
            self._require_write_ports()
            return await self.propose_from_bridge(arguments, context)
        if name == "rule.operator_syntax_fix":
            self._require_write_ports()
            return await self.operator_syntax_fix(arguments, context)
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

    async def _resolve_article_claim_motifs(
        self,
        context: ToolExecutionContext,
        motifs: Sequence[RuleMotif],
    ) -> tuple[RuleMotif, ...]:
        claim_ids = tuple(
            sorted({claim_id for motif in motifs for claim_id in motif.article_claim_refs})
        )
        if not claim_ids:
            return tuple(motifs)
        refs_by_claim: dict[str, UUID] = {}
        if self.evidence_observations is not None:
            try:
                prior_observations = await self.evidence_observations.list_for_investigation(
                    context.investigation_id
                )
            except Exception as exc:
                raise _ToolUnavailable("article_claim_citation_unavailable") from exc
            refs_by_claim = {
                observation.object_id: observation.id
                for observation in prior_observations
                if observation.kind is EvidenceKind.CLAIM_SOURCE
                and observation.source == "AutoWork verified article snapshot"
                and observation.source_version == "investigation-rule-article-claim.v1"
                and observation.object_type == "article_claim_citation"
                and observation.object_id in claim_ids
            }
        unresolved_ids = tuple(item for item in claim_ids if item not in refs_by_claim)
        if unresolved_ids and (self.article_claims is None or self.recorder is None):
            raise _ToolUnavailable("article_claim_citation_unavailable")
        recorder = self.recorder
        try:
            claims = (
                tuple(
                    await self.article_claims.get_claims(context.investigation_id, unresolved_ids)
                )
                if unresolved_ids and self.article_claims is not None
                else ()
            )
        except Exception as exc:
            raise _ToolUnavailable("article_claim_citation_unavailable") from exc
        by_id = {getattr(claim, "id", None): claim for claim in claims}
        if any(claim_id not in by_id for claim_id in unresolved_ids):
            raise _ToolFailed("article_claim_reference_not_found")

        if unresolved_ids and recorder is None:
            raise _ToolUnavailable("article_claim_citation_unavailable")
        for claim_id in unresolved_ids:
            claim = by_id[claim_id]
            statement = getattr(claim, "statement", None)
            source_refs = getattr(claim, "source_refs", None)
            if (
                not isinstance(statement, str)
                or not statement.strip()
                or not isinstance(source_refs, Sequence)
                or isinstance(source_refs, str | bytes)
                or any(not isinstance(item, str) for item in source_refs)
            ):
                raise _ToolFailed("article_claim_reference_invalid")
            payload = {
                "schema": "investigation-rule-article-claim.v1",
                "claim_id": claim_id,
                "statement": statement,
                "source_refs": list(source_refs),
            }
            claim_sha256 = hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            assert recorder is not None
            observation = await recorder.record_json(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.CLAIM_SOURCE,
                payload=payload,
                source="AutoWork verified article snapshot",
                source_version="investigation-rule-article-claim.v1",
                object_type="article_claim_citation",
                object_id=claim_id,
                limitations=(
                    "Citation to a claim in the verified article snapshot; this is not sample "
                    "behavioral evidence.",
                ),
                idempotency_key=(
                    f"rule-article-claim:{context.investigation_id}:{claim_id}:{claim_sha256}"
                ),
            )
            refs_by_claim[claim_id] = observation.id

        return tuple(
            replace(
                motif,
                observation_refs=tuple(
                    dict.fromkeys(
                        (
                            *motif.observation_refs,
                            *(refs_by_claim[claim_id] for claim_id in motif.article_claim_refs),
                        )
                    )
                ),
            )
            for motif in motifs
        )

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
        raw_motifs = _empty_optional_value(arguments.get("motifs"))
        motifs = await self._resolve_article_claim_motifs(
            context, _motifs_from_json(raw_motifs or ())
        )
        raw_feature_refs = _empty_optional_value(arguments.get("feature_refs"))
        feature_refs = tuple(UUID(value) for value in (raw_feature_refs or ()))
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
        if any(not motif.observation_refs for motif in generated):
            raise _ToolFailed("motifs_require_observation_refs")
        candidate_id_value = _empty_optional_value(arguments.get("candidate_id"))
        candidate_id = UUID(candidate_id_value) if candidate_id_value is not None else uuid4()
        condition = arguments["condition"]
        meta = _normalize_meta(arguments.get("meta"))
        all_versions = await self.candidates.list_for_candidate(candidate_id)
        existing_versions = tuple(
            item for item in all_versions if item.investigation_id == context.investigation_id
        )
        if existing_versions:
            validation = self.workbench.validate_motifs(generated, meta=meta, condition=condition)
            if not validation.valid:
                raise _ToolFailed("motif_validation_failed")
            source_validation = await self.workbench.verify_dotnet_source_motifs(generated)
            if not source_validation.valid:
                raise _ToolFailed("motif_source_unverified")
            verified_source_motifs = {
                generated[index] for index in source_validation.verified_motif_indices
            }
            if any(not motif.observation_refs for motif in generated):
                raise _ToolFailed("motifs_require_observation_refs")
            build = self.workbench.build_rule(generated, condition, meta)
            if not build.valid or build.source is None:
                raise _ToolFailed("rule_build_failed")
            latest = max(existing_versions, key=lambda item: item.version)
            requested_sha256 = hashlib.sha256(build.source.encode("utf-8")).hexdigest()
            expected_provenance = tuple(
                MotifProvenance(
                    motif=motif.value,
                    rationale=_serialize_motif(
                        motif,
                        source_bytes_verified=motif in verified_source_motifs,
                    ),
                    observation_refs=motif.observation_refs,
                )
                for motif in sorted(
                    generated,
                    key=lambda item: (item.kind.value, item.value.casefold(), item.provenance_id),
                )
            )
            if (
                latest.source_sha256 != requested_sha256
                or latest.motif_provenance != expected_provenance
            ):
                raise _ToolFailed("candidate_id_already_exists_with_different_proposal")
            latest_test = await self._latest_explicit_test_report(context, latest)
            return (
                {
                    "candidate_id": str(latest.candidate_id),
                    "version_id": str(latest.id),
                    "version": latest.version_label,
                    "parent_version_id": (
                        str(latest.parent_version_id) if latest.parent_version_id else None
                    ),
                    "source_sha256": latest.source_sha256,
                    "candidate_status": latest.status.value,
                    "validation_status": (
                        _rule_validation_status(latest_test)
                        if latest_test is not None
                        else RuleValidationStatus.UNTESTED.value
                    ),
                    "validation_reason": (
                        latest_test.get("validation_reason")
                        if latest_test is not None
                        else RuleValidationReason.EXPLICIT_TEST_REQUIRED.value
                    ),
                    "evaluation_status": ToolInvocationStatus.SUCCEEDED.value,
                    "observation_refs": [],
                    "replayed": True,
                },
                ToolInvocationStatus.SUCCEEDED,
                (),
            )
        if all_versions:
            raise _ToolFailed("candidate_id_already_exists")
        result = await self._create_version(
            context,
            candidate_id=candidate_id,
            parent=None,
            motifs=generated,
            condition=condition,
            meta=meta,
        )
        output = dict(self._version_output(result))
        status = (
            ToolInvocationStatus.SUCCEEDED
            if result.compile_check.status is YaraExecutionStatus.OK
            else ToolInvocationStatus.PARTIAL
        )
        return output, status, ()

    async def propose_from_bridge(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        assert self.workbench is not None and self.candidates is not None
        if self.evidence_observations is None or self.evidence_payloads is None:
            raise _ToolUnavailable("bridge_rule_provenance_unavailable")
        observation_id = UUID(arguments["bridge_observation_id"])
        requested_invocation_id = UUID(arguments["bridge_invocation_id"])
        requested_sha256 = arguments["rule_source_sha256"]
        observation = await self.evidence_observations.get(observation_id)
        if (
            observation is None
            or observation.investigation_id != context.investigation_id
            or observation.kind is not EvidenceKind.OTHER
            or observation.source != "advisor.consult"
            or observation.object_type != "advisor_call"
            or observation.object_id != "yara_draft"
        ):
            raise _ToolFailed("bridge_yara_observation_not_found")
        try:
            persisted = await self.evidence_payloads.read_json(observation.blob_id)
        except Exception as exc:
            raise _ToolFailed("bridge_yara_observation_unreadable") from exc
        if not isinstance(persisted, Mapping):
            raise _ToolFailed("bridge_yara_observation_invalid")
        output = persisted.get("output") if isinstance(persisted, Mapping) else None
        bridge_provenance = output.get("bridge_provenance") if isinstance(output, Mapping) else None
        persisted_invocation_id = (
            persisted.get("bridge_invocation_id") if isinstance(persisted, Mapping) else None
        )
        reparsed_from = persisted.get("reparsed_from_observation_id")
        reparsed = (
            observation.source_version == ADVISOR_SOURCE_PARSER_VERSION
            and persisted.get("parser_version") == ADVISOR_SOURCE_PARSER_VERSION
            and isinstance(reparsed_from, str)
        )
        parse_status_allowed = (
            persisted.get("advisor_status") == "succeeded"
            and isinstance(output, Mapping)
            and output.get("status") == "succeeded"
            and output.get("advisor_status") == "succeeded"
        )
        if reparsed:
            try:
                original_id = UUID(reparsed_from)
                original = await self.evidence_observations.get(original_id)
                original_payload = (
                    await self.evidence_payloads.read_json(original.blob_id)
                    if original is not None
                    else None
                )
            except Exception as exc:
                raise _ToolFailed("bridge_yara_reparse_link_unverified") from exc
            original_output = (
                original_payload.get("output") if isinstance(original_payload, Mapping) else None
            )
            original_provenance = (
                original_output.get("bridge_provenance")
                if isinstance(original_output, Mapping)
                else None
            )
            if (
                original is None
                or original.id == observation.id
                or original.investigation_id != context.investigation_id
                or original.kind is not EvidenceKind.OTHER
                or original.source != "advisor.consult"
                or original.object_type != "advisor_call"
                or original.object_id != "yara_draft"
                or original.source_version == ADVISOR_SOURCE_PARSER_VERSION
                or not isinstance(original_payload, Mapping)
                or original_payload.get("schema") != "operator-advisor-call.v1"
                or original_payload.get("reparsed_from_observation_id") is not None
                or original_payload.get("output_sha256") != persisted.get("output_sha256")
                or original_payload.get("input_refs") != persisted.get("input_refs")
                or original_payload.get("measurement_catalog")
                != persisted.get("measurement_catalog")
                or original_payload.get("bridge_invocation_id") != persisted_invocation_id
                or not isinstance(original_provenance, Mapping)
                or original_provenance.get("bridge_invocation_id") != persisted_invocation_id
                or original_provenance.get("output_sha256") != persisted.get("output_sha256")
                or not isinstance(bridge_provenance, Mapping)
                or original_provenance.get("measurement_catalog")
                != bridge_provenance.get("measurement_catalog")
                or not isinstance(output, Mapping)
                or output.get("status") not in {"partial", "succeeded"}
                or output.get("advisor_status") not in {"partial", "succeeded"}
                or persisted.get("advisor_status") not in {"partial", "succeeded"}
            ):
                raise _ToolFailed("bridge_yara_reparse_link_unverified")
            parse_status_allowed = True
        if (
            persisted.get("schema") != "operator-advisor-call.v1"
            or persisted.get("consultation_type") != "yara_draft"
            or not parse_status_allowed
            or persisted.get("advisor_response_schema") != "advisor-response.v3"
            or not isinstance(output, Mapping)
            or output.get("schema") != "advisor-response.v3"
            or output.get("consultation_type") != "yara_draft"
            or output.get("usable") is not True
            or not isinstance(bridge_provenance, Mapping)
            or bridge_provenance.get("bridge_invocation_id") != str(requested_invocation_id)
            or bridge_provenance.get("measurement_catalog") != persisted.get("measurement_catalog")
            or (
                reparsed
                and (
                    bridge_provenance.get("output_sha256") != persisted.get("output_sha256")
                    or bridge_provenance.get("input_refs") != persisted.get("input_refs")
                )
            )
            or persisted_invocation_id != str(requested_invocation_id)
        ):
            raise _ToolFailed("bridge_yara_provenance_not_succeeded")
        if output.get("truncation_status") or output.get("truncated") is True:
            raise _ToolFailed("bridge_yara_source_truncated")
        source = output.get("rule_source")
        observed_sha256 = output.get("rule_source_sha256")
        if not isinstance(source, str) or not source.strip():
            raise _ToolFailed("bridge_yara_source_missing_or_truncated")
        if (
            observed_sha256 != requested_sha256
            or hashlib.sha256(source.encode("utf-8")).hexdigest() != observed_sha256
        ):
            raise _ToolFailed("bridge_yara_source_sha256_mismatch")
        source_issues = validate_single_rule_candidate(source)
        if source_issues:
            raise _ToolFailed("bridge_yara_source_rejected:" + ",".join(source_issues))
        raw_measurement_catalog = bridge_provenance.get("measurement_catalog")
        measurement_catalog = _bridge_measurement_catalog(raw_measurement_catalog)
        if measurement_catalog is None:
            raise _ToolFailed("bridge_yara_measurement_catalog_missing")

        raw_motifs = output.get("motifs")
        if (
            not isinstance(raw_motifs, Sequence)
            or isinstance(raw_motifs, str | bytes)
            or not raw_motifs
        ):
            raise _ToolFailed("bridge_yara_motif_refs_missing")
        input_refs = persisted.get("input_refs", ())
        if not isinstance(input_refs, Sequence) or isinstance(input_refs, str | bytes):
            raise _ToolFailed("bridge_yara_input_refs_invalid")
        source_observation_refs: list[UUID] = []
        transmitted_measurements: set[tuple[str, str]] = set()
        for reference in input_refs:
            if not isinstance(reference, Mapping):
                continue
            if reference.get("kind") == "measurement":
                measurement_id = reference.get("id")
                measurement_sha256 = reference.get("sha256")
                if (
                    not isinstance(measurement_id, str)
                    or not measurement_id.strip()
                    or not isinstance(measurement_sha256, str)
                    or re.fullmatch(_SHA256_PATTERN, measurement_sha256) is None
                ):
                    raise _ToolFailed("bridge_yara_input_ref_invalid")
                transmitted_measurements.add((measurement_id, measurement_sha256))
                continue
            if reference.get("kind") != "observation":
                continue
            try:
                source_ref = UUID(str(reference["id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise _ToolFailed("bridge_yara_input_ref_invalid") from exc
            source_observation = await self.evidence_observations.get(source_ref)
            if (
                source_observation is None
                or source_observation.investigation_id != context.investigation_id
                or source_observation.sha256 != reference.get("sha256")
            ):
                raise _ToolFailed("bridge_yara_input_ref_not_verified")
            source_observation_refs.append(source_ref)
        if not source_observation_refs:
            raise _ToolFailed("bridge_yara_measurement_observation_missing")

        declared_names: set[str] = set()
        motif_provenance: list[MotifProvenance] = []
        observation_refs = tuple(dict.fromkeys((*source_observation_refs, observation.id)))
        source_attribution = {
            "author": "bridge",
            "change_kind": "bridge_yara_draft",
            "bridge_invocation_id": str(requested_invocation_id),
            "bridge_observation_id": str(observation.id),
            "bridge_rule_source_sha256": observed_sha256,
        }
        for raw_motif in raw_motifs:
            if not isinstance(raw_motif, Mapping):
                raise _ToolFailed("bridge_yara_motif_ref_invalid")
            name = raw_motif.get("name")
            measurement_refs = raw_motif.get("measurement_refs")
            if (
                not isinstance(name, str)
                or re.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", name) is None
                or name in declared_names
                or not isinstance(measurement_refs, Sequence)
                or isinstance(measurement_refs, str | bytes)
                or not measurement_refs
            ):
                raise _ToolFailed("bridge_yara_motif_ref_invalid")
            declared_names.add(name)
            for reference in measurement_refs:
                if (
                    not isinstance(reference, Mapping)
                    or not isinstance(reference.get("measurement_id"), str)
                    or not isinstance(reference.get("sha256"), str)
                    or re.fullmatch(_SHA256_PATTERN, reference["sha256"]) is None
                ):
                    raise _ToolFailed("bridge_yara_motif_ref_invalid")
                if (
                    reference["measurement_id"],
                    reference["sha256"],
                ) not in transmitted_measurements:
                    raise _ToolFailed("bridge_yara_measurement_ref_not_transmitted")
                measured = measurement_catalog.get(
                    (reference["measurement_id"], reference["sha256"])
                )
                expected_bytes = _bridge_measurement_bytes(measured) if measured else None
                actual_bytes = _exact_yara_string_bytes(source, name)
                if expected_bytes is None or actual_bytes != expected_bytes:
                    measurement = measured or {}
                    raise _ToolFailed(
                        "motif_bytes_mismatch",
                        agent_message=(
                            f"Bridge motif {name} does not exactly match its cited measurement."
                        ),
                        diagnostic={
                            "motif": name,
                            "measurement_id": reference["measurement_id"],
                            "measurement_alias": measurement.get("alias"),
                            "sample_sha256": measurement.get("sample_sha256"),
                            "offset": measurement.get("offset"),
                            "length": measurement.get("length"),
                            "encoding": measurement.get("encoding"),
                            "reason": (
                                "measurement_bytes_unavailable"
                                if expected_bytes is None
                                else "pattern_bytes_differ"
                            ),
                        },
                    )
            motif = RuleMotif(
                kind=RuleMotifKind.STRING,
                value=name,
                provenance_kind=RuleMotifProvenanceKind.BRIDGE,
                provenance_id=str(requested_invocation_id),
                observation_refs=observation_refs,
                rationale="Bridge v3 motif refs: "
                + json.dumps(measurement_refs, sort_keys=True, separators=(",", ":")),
            )
            motif_provenance.append(
                MotifProvenance(
                    motif=name,
                    rationale=_serialize_motif_with_source(motif, source_attribution),
                    observation_refs=observation_refs,
                )
            )
        source_strings = re.search(r"(?is)\bstrings\s*:\s*(.*?)\bcondition\s*:", source)
        source_names = set(
            re.findall(
                r"(\$[A-Za-z_][A-Za-z0-9_]*)\s*=",
                source_strings.group(1) if source_strings else "",
            )
        )
        condition_match = re.search(r"(?is)\bcondition\s*:\s*(.*?)(?:\}\s*)?$", source)
        condition_text = condition_match.group(1) if condition_match else ""
        condition_names = set(re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*", condition_text))
        all_patterns_referenced = (
            re.search(r"(?i)\b(?:any|all|\d+)\s+of\s+them\b", condition_text) is not None
        )
        if declared_names != source_names or not (
            all_patterns_referenced or declared_names.issubset(condition_names)
        ):
            raise _ToolFailed("bridge_yara_motifs_do_not_cover_rule_source")

        candidate_value = _empty_optional_value(arguments.get("candidate_id"))
        candidate_id = UUID(candidate_value) if candidate_value is not None else uuid4()
        if await self.candidates.list_for_candidate(candidate_id):
            raise _ToolFailed("candidate_id_already_exists")
        corpus = await self._build_corpus(context.investigation_id)
        compile_check = self.workbench.compile_check(source)
        version = DetectionCandidateVersion(
            investigation_id=context.investigation_id,
            candidate_id=candidate_id,
            source=source,
            source_sha256=observed_sha256,
            motif_provenance=tuple(motif_provenance),
            status=DetectionCandidateStatus.CANDIDATE,
            version=1,
            test_refs=tuple(dict.fromkeys(case.id for case in corpus.cases)),
        )
        stored = await self.candidates.append_version(version, expected_previous_id=None)
        output_value = {
            "candidate_id": str(stored.candidate_id),
            "version_id": str(stored.id),
            "version": stored.version_label,
            "parent_version_id": None,
            "source_sha256": stored.source_sha256,
            "candidate_status": stored.status.value,
            "source_author": "bridge",
            "source_change_kind": "bridge_yara_draft",
            "final_status": OneRuleTestStatus.NOT_VALIDATED.value,
            "validation_reasons": ["explicit_rule_test_required"],
            "bridge_invocation_id": str(requested_invocation_id),
            "bridge_observation_id": str(observation.id),
            "bridge_rule_source_sha256": observed_sha256,
            "compile_status": compile_check.status.value,
            "test_refs": [str(reference) for reference in stored.test_refs],
            "evaluation_status": (
                ToolInvocationStatus.SUCCEEDED.value
                if compile_check.status is YaraExecutionStatus.OK
                else ToolInvocationStatus.PARTIAL.value
            ),
        }
        return (
            output_value,
            ToolInvocationStatus.SUCCEEDED
            if compile_check.status is YaraExecutionStatus.OK
            else ToolInvocationStatus.PARTIAL,
            (observation.id, *source_observation_refs),
        )

    async def operator_syntax_fix(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        assert self.workbench is not None and self.candidates is not None
        candidate_id = UUID(arguments["candidate_id"])
        versions = await self._versions_for_investigation(candidate_id, context.investigation_id)
        parent = next(
            (item for item in versions if item.version == arguments["parent_version"]), None
        )
        if parent is None:
            raise _ToolFailed("parent_version_not_found")
        if not versions or parent.id != versions[-1].id:
            raise _ToolFailed("parent_must_be_latest")
        if parent.status is DetectionCandidateStatus.APPROVED:
            raise _ToolFailed("approved_versions_are_immutable")
        attribution = _source_attribution_from_version(parent)
        if attribution is None or attribution.get("change_kind") not in {
            "bridge_yara_draft",
            "operator_syntax_fix",
        }:
            raise _ToolFailed("operator_syntax_fix_requires_bridge_source")
        author = sanitize_evidence_text(arguments["author"], max_chars=256).strip()
        source = arguments["source"]
        if not author or not isinstance(source, str) or not source.strip():
            raise _ToolFailed("operator_syntax_fix_metadata_invalid")
        source_issues = validate_single_rule_candidate(source)
        if source_issues:
            raise _ToolFailed("operator_syntax_fix_source_rejected:" + ",".join(source_issues))
        if source == parent.source:
            raise _ToolFailed("operator_syntax_fix_requires_source_change")
        _require_syntax_only_fix(parent.source, source)
        diff = "".join(
            unified_diff(
                parent.source.splitlines(keepends=True),
                source.splitlines(keepends=True),
                fromfile=parent.source_sha256,
                tofile=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            )
        )
        changed_lines = sum(
            line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            for line in diff.splitlines()
        )
        if changed_lines > 12:
            raise _ToolFailed("operator_syntax_fix_not_minimal")
        compile_check = self.workbench.compile_check(source)
        if compile_check.status not in {
            YaraExecutionStatus.OK,
            YaraExecutionStatus.ENGINE_UNAVAILABLE,
        }:
            raise _ToolFailed("operator_syntax_fix_compile_failed")
        next_attribution = {
            **attribution,
            "author": author,
            "change_kind": "operator_syntax_fix",
            "operator_parent_source_sha256": parent.source_sha256,
            "operator_diff": diff,
        }
        provenance = tuple(
            MotifProvenance(
                motif=item.motif,
                rationale=_add_source_attribution(item.rationale, next_attribution),
                observation_refs=item.observation_refs,
            )
            for item in parent.motif_provenance
        )
        version = DetectionCandidateVersion(
            investigation_id=context.investigation_id,
            candidate_id=candidate_id,
            source=source,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            motif_provenance=provenance,
            status=DetectionCandidateStatus.CANDIDATE,
            version=parent.version + 1,
            parent_version_id=parent.id,
            test_refs=parent.test_refs,
        )
        stored = await self.candidates.append_version(version, expected_previous_id=parent.id)
        return (
            {
                "candidate_id": str(stored.candidate_id),
                "version_id": str(stored.id),
                "version": stored.version_label,
                "parent_version_id": str(parent.id),
                "source_sha256": stored.source_sha256,
                "source_author": author,
                "source_change_kind": "operator_syntax_fix",
                "source_diff": diff,
                "final_status": OneRuleTestStatus.NOT_VALIDATED.value,
                "validation_reasons": ["explicit_rule_test_required"],
                "compile_status": compile_check.status.value,
                "test_refs": [str(reference) for reference in stored.test_refs],
            },
            ToolInvocationStatus.SUCCEEDED
            if compile_check.status is YaraExecutionStatus.OK
            else ToolInvocationStatus.PARTIAL,
            tuple(
                dict.fromkeys(
                    ref for motif in parent.motif_provenance for ref in motif.observation_refs
                )
            ),
        )

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
        source_attribution = _source_attribution_from_version(parent)
        if source_attribution is not None and source_attribution.get("change_kind") in {
            "bridge_yara_draft",
            "operator_syntax_fix",
        }:
            raise _ToolFailed("bridge_source_requires_operator_syntax_fix_or_bridge_redraft")
        previous_test = await self._latest_explicit_test_report(context, parent)
        if previous_test is None:
            raise _ToolFailed(
                "rule_revision_requires_test",
                agent_message=(
                    "Run rule.test for this exact candidate and version before revising it. "
                    "A proposal compile check is not a test."
                ),
            )
        if not _report_demonstrates_false_negative(previous_test):
            raise _ToolFailed(
                "rule_revision_not_justified",
                agent_message=(
                    "Aucune révision justifiée : le dernier rule.test ne démontre aucun faux "
                    "négatif parmi les positifs confirmés du corpus contrôlé."
                ),
            )
        raw_motifs = _empty_optional_value(arguments.get("motifs"))
        motifs = _motifs_from_json(raw_motifs) if raw_motifs is not None else None
        condition = _empty_optional_value(arguments.get("condition"))
        if motifs is None and condition is None:
            raise _ToolFailed("revision_requires_a_change")
        if motifs is None:
            motifs = _motifs_from_version(parent)
        else:
            motifs = await self._resolve_article_claim_motifs(context, motifs)
            if any(not motif.observation_refs for motif in motifs):
                raise _ToolFailed("motifs_require_observation_refs")
        if condition is None:
            condition = _condition_from_source(parent.source)
        meta = _normalize_meta(arguments.get("meta"))
        if meta is None:
            meta = _meta_from_source(parent.source)
        result = await self._create_version(
            context,
            candidate_id=candidate_id,
            parent=parent,
            motifs=motifs,
            condition=condition,
            meta=meta,
        )
        output = dict(self._version_output(result))
        output["revision_basis"] = {
            "measured_defect": "confirmed_positive_false_negative",
            "parent_version": parent.version,
            "parent_source_sha256": parent.source_sha256,
            "test_corpus_sha256": previous_test.get("corpus_sha256"),
            "false_negative_sha256s": previous_test.get("false_negative_sha256s", []),
        }
        status = (
            ToolInvocationStatus.SUCCEEDED
            if result.compile_check.status is YaraExecutionStatus.OK
            else ToolInvocationStatus.PARTIAL
        )
        return output, status, ()

    async def revise_with_added_motifs(
        self,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        """Append bridge motifs to the latest immutable version via the rule path."""
        assert self.candidates is not None
        candidate_id = UUID(arguments["candidate_id"])
        versions = await self._versions_for_investigation(candidate_id, context.investigation_id)
        parent_number = arguments["parent_version"]
        parent = next((item for item in versions if item.version == parent_number), None)
        if parent is None or not versions or parent.id != versions[-1].id:
            raise _ToolFailed("parent_must_be_latest")
        existing = _motifs_from_version(parent)
        additions = _motifs_from_json(arguments["motifs"])
        combined: dict[tuple[str, str, str, str], RuleMotif] = {}
        for motif in (*existing, *additions):
            combined.setdefault(
                (
                    motif.kind.value,
                    motif.value.casefold(),
                    motif.provenance_kind.value,
                    motif.provenance_id,
                ),
                motif,
            )
        if len(combined) == len(existing):
            raise _ToolFailed("revision_adds_no_new_motifs")
        revised_arguments = dict(arguments)
        revised_arguments["motifs"] = [_motif_payload(item) for item in combined.values()]
        try:
            return await self.revise(revised_arguments, context)
        except _ToolFailed as exc:
            code = _bounded_reason(str(exc))
            return (
                {
                    "error_code": code,
                    "message": exc.agent_message
                    or (
                        "Rule revision was refused. Check the candidate version and its "
                        "explicit test report before retrying."
                    ),
                },
                ToolInvocationStatus.FAILED,
                (),
            )

    async def test(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]:
        assert self.workbench is not None and self.candidates is not None
        try:
            candidate_id = UUID(arguments["candidate_id"])
            versions = await self._versions_for_investigation(
                candidate_id, context.investigation_id
            )
            selected = self._select_version(
                versions, _empty_optional_value(arguments.get("version"))
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise _RuleTestInputFailure("candidate_or_version_validation", exc) from exc
        try:
            corpus = await self._build_corpus(context.investigation_id, tested_version=selected)
        except (ValueError, TypeError, KeyError) as exc:
            raise _RuleTestInputFailure("corpus_validation", exc) from exc
        try:
            report = await self.workbench.compile_and_test(
                selected.source,
                corpus.cases,
                selection_bias=corpus.selection_bias,
                coverage_statement=corpus.coverage_statement,
                investigation_id=context.investigation_id,
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise _RuleTestInputFailure("yara_input_validation", exc) from exc
        corpus = _corpus_with_measurement_blockers(corpus, report)
        evaluation = _status_for_report(report, corpus)
        signature = _signature_manifest(selected)
        # Candidate lifecycle records the measured outcome. Signature provenance and
        # corpus limitations affect validation quality, not whether this test ran.
        candidate_status = _candidate_status(report, corpus)
        validation_status, validation_reason = _effective_validation_quality(report, signature)
        final_status, validation_reasons = _one_rule_final_status(report, corpus, selected)
        if (
            final_status is OneRuleTestStatus.NOT_VALIDATED
            and validation_status is RuleValidationStatus.REAL_VALIDATED
        ):
            validation_status = RuleValidationStatus.REAL_PARTIAL
            validation_reason = RuleValidationReason.EXECUTION_INCOMPLETE
        try:
            observation = await self._record_report(
                context,
                report,
                corpus,
                candidate_id=selected.candidate_id,
                version=selected,
                candidate_status=candidate_status,
                operation="test",
            )
        except Exception as exc:
            raise _ToolFailed(
                "report_persistence",
                agent_message=(
                    "YARA testing completed, but the complete report was not persisted. "
                    "No validation result is available until the report blob is stored."
                ),
                diagnostic={
                    "stage": "report_persistence",
                    "exception_class": _safe_exception_type_name(exc),
                },
            ) from exc
        report_summary = _bounded_report_summary(
            _report_summary(report, observation.id, corpus, selected, candidate_status),
            observation_id=observation.id,
            report_blob_id=observation.blob_id,
            report_sha256=observation.sha256,
        )
        return (
            {
                "candidate_id": str(selected.candidate_id),
                "version_id": str(selected.id),
                "version": selected.version_label,
                "source_sha256": selected.source_sha256,
                "candidate_status": candidate_status.value,
                "validation_status": validation_status.value,
                "validation_reason": validation_reason.value,
                "final_status": final_status.value,
                "validation_reasons": list(validation_reasons),
                "report": report_summary,
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
        output = _comparison_summary(comparison, observation.id, corpus, version_a, version_b)
        output["finding_candidate"] = {
            "kind": "detection",
            "assessment": "candidate",
            "summary": (
                f"{version_b.version_label} changes raw-byte matches relative to "
                f"{version_a.version_label}; this does not confirm a family."
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
        selected_version = _empty_optional_value(arguments.get("version"))
        selected = (
            self._select_version(versions, selected_version)
            if selected_version is not None
            else versions[-1]
        )
        include_history = bool(_empty_optional_value(arguments.get("include_history")))
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
        report_observations.sort(key=lambda item: (item.observed_at, item.id.hex))
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
    ) -> _CreatedRuleCandidate:
        assert self.workbench is not None and self.candidates is not None
        validation = self.workbench.validate_motifs(motifs, meta=meta, condition=condition)
        if not validation.valid:
            raise _ToolFailed("motif_validation_failed")
        source_validation = await self.workbench.verify_dotnet_source_motifs(motifs)
        if not source_validation.valid:
            raise _ToolFailed("motif_source_unverified")
        verified_source_motifs = {
            motifs[index] for index in source_validation.verified_motif_indices
        }
        if any(not motif.observation_refs for motif in motifs):
            raise _ToolFailed("motifs_require_observation_refs")
        build = self.workbench.build_rule(motifs, condition, meta)
        if not build.valid or build.source is None:
            raise _ToolFailed("rule_build_failed")
        compile_check = self.workbench.compile_check(build.source)
        if compile_check.status is YaraExecutionStatus.COMPILE_ERROR:
            line = f" at line {compile_check.line}" if compile_check.line else ""
            raise _ToolFailed(
                "rule_syntax_invalid",
                agent_message=(
                    f"Generated YARA syntax did not compile{line}. Correct the motif or "
                    "condition and propose again."
                ),
            )
        if compile_check.status is YaraExecutionStatus.ENGINE_UNAVAILABLE:
            if not self.workbench.has_confined_test_runner:
                raise _ToolUnavailable("rule_compile_check_unavailable")
        elif compile_check.status is not YaraExecutionStatus.OK:
            line = f" at line {compile_check.line}" if compile_check.line else ""
            raise _ToolFailed(
                "rule_compile_check_failed",
                agent_message=(
                    f"YARA compile check could not complete{line} "
                    f"({compile_check.status.value}). Retry once, then report this limitation."
                ),
            )
        version_number = 1 if parent is None else parent.version + 1
        parent_id = parent.id if parent is not None else None
        version = DetectionCandidateVersion(
            investigation_id=context.investigation_id,
            candidate_id=candidate_id,
            source=build.source,
            source_sha256=hashlib.sha256(build.source.encode("utf-8")).hexdigest(),
            motif_provenance=tuple(
                MotifProvenance(
                    motif=motif.value,
                    rationale=_serialize_motif(
                        motif,
                        source_bytes_verified=motif in verified_source_motifs,
                    ),
                    observation_refs=motif.observation_refs,
                )
                for motif in sorted(
                    motifs,
                    key=lambda item: (item.kind.value, item.value.casefold(), item.provenance_id),
                )
            ),
            status=DetectionCandidateStatus.CANDIDATE,
            version=version_number,
            parent_version_id=parent_id,
            test_refs=tuple(
                dict.fromkeys(
                    case.id for case in (await self._build_corpus(context.investigation_id)).cases
                )
            ),
        )
        stored = await self.candidates.append_version(version, expected_previous_id=parent_id)
        return _CreatedRuleCandidate(version=stored, compile_check=compile_check)

    def _version_output(
        self,
        result: _CreatedRuleCandidate,
    ) -> Mapping[str, Any]:
        version = result.version
        return {
            "candidate_id": str(version.candidate_id),
            "version_id": str(version.id),
            "version": version.version_label,
            "parent_version_id": (
                str(version.parent_version_id) if version.parent_version_id else None
            ),
            "source_sha256": version.source_sha256,
            "candidate_status": version.status.value,
            "validation_status": RuleValidationStatus.UNTESTED.value,
            "validation_reason": RuleValidationReason.EXPLICIT_TEST_REQUIRED.value,
            "final_status": OneRuleTestStatus.NOT_VALIDATED.value,
            "validation_reasons": ["explicit_rule_test_required"],
            "evaluation_status": (
                ToolInvocationStatus.SUCCEEDED.value
                if result.compile_check.status is YaraExecutionStatus.OK
                else ToolInvocationStatus.PARTIAL.value
            ),
            "compile_check": {
                "status": result.compile_check.status.value,
                "engine_version": result.compile_check.engine_version[:200],
                "line": result.compile_check.line,
                "purpose": "syntax_only; rule.test has not run",
            },
            "signature": _signature_manifest(version),
            "observation_refs": [],
        }

    async def _latest_explicit_test_report(
        self, context: ToolExecutionContext, version: DetectionCandidateVersion
    ) -> Mapping[str, Any] | None:
        if self.evidence_observations is None or self.evidence_payloads is None:
            return None
        observations = await self.evidence_observations.list_for_investigation(
            context.investigation_id
        )
        matching = sorted(
            (
                observation
                for observation in observations
                if observation.kind is EvidenceKind.YARA_MATCH
                and observation.object_type == "detection_candidate_version"
                and observation.object_id == str(version.id)
            ),
            key=lambda item: (item.observed_at, item.id.hex),
            reverse=True,
        )
        for observation in matching:
            try:
                payload = await self.evidence_payloads.read_json(observation.blob_id)
            except Exception:
                continue
            if (
                isinstance(payload, Mapping)
                and payload.get("schema") == "investigation-yara-test.v1"
                and payload.get("operation") == "test"
                and payload.get("candidate_id") == str(version.candidate_id)
                and payload.get("version_id") == str(version.id)
                and payload.get("source_sha256") == version.source_sha256
            ):
                return payload
        return None

    async def _build_corpus(
        self,
        investigation_id: UUID,
        *,
        tested_version: DetectionCandidateVersion | None = None,
    ) -> _RuleCorpusSnapshot:
        assert self.associations is not None
        limitations: list[str] = []
        blocking_limitations: list[str] = []
        measurement_limitations: list[str] = []

        def add_limitation(message: str, *, blocking: bool) -> None:
            limitations.append(message)
            (blocking_limitations if blocking else measurement_limitations).append(message)

        manual_import_reader = getattr(self.corpus_inventory, "manual_rule_corpus_imports", None)
        manual_import_read_failed = False
        try:
            manual_imports = tuple(
                await manual_import_reader(investigation_id)
                if callable(manual_import_reader)
                else ()
            )
        except Exception:
            manual_imports = ()
            manual_import_read_failed = True
            add_limitation(
                "L'inventaire des imports manuels du corpus n'a pas pu être lu.", blocking=True
            )
        all_manual_imports = manual_imports
        manual_import_hashes = {item.sample_sha256 for item in all_manual_imports}
        if tested_version is not None:
            late_holdouts = tuple(
                item
                for item in all_manual_imports
                if item.role in _HOLDOUT_ROLES_FOR_TEST
                and (item.designated_at is None or item.designated_at >= tested_version.created_at)
            )
            if late_holdouts:
                add_limitation(
                    "Des holdouts désignés à la date ou après la version testée ont été exclus.",
                    blocking=True,
                )
            manual_imports = tuple(item for item in all_manual_imports if item not in late_holdouts)
        associations = await self.associations.list_for_investigation(investigation_id)
        by_hash: dict[str, list[SampleAssociation]] = {}
        for association in associations:
            by_hash.setdefault(association.sample_sha256, []).append(association)
        cases: dict[tuple[RuleTestCorpusRole, str], RuleTestCase] = {}

        sample_formats: dict[str, str] = {}

        async def bytes_available(sample_sha256: str) -> bool:
            if self.corpus_bytes is None:
                return False
            try:
                resolver = getattr(self.corpus_bytes, "get_artifact_id_for_sample", None)
                if not callable(resolver):
                    return False
                artifact_id = await resolver(sample_sha256)
                if not isinstance(artifact_id, UUID):
                    return False
            except Exception:
                return False
            return True

        def record_missing_bytes(sample_sha256: str, declared_role: RuleTestCorpusRole) -> None:
            add_limitation(
                f"Les octets du hash {sample_sha256}, déclaré dans le rôle "
                f"{declared_role.value}, sont indisponibles; ce rôle n'est pas entièrement "
                "mesurable.",
                blocking=True,
            )

        for sample_sha256, matches in by_hash.items():
            if sample_sha256 in manual_import_hashes:
                continue
            if any(item.qualification is SampleQualification.CONFIRMED for item in matches):
                role = RuleTestCorpusRole.HUMAN_CONFIRMED
            elif any(
                item.qualification in {SampleQualification.REJECTED, SampleQualification.UNCERTAIN}
                for item in matches
            ):
                role = RuleTestCorpusRole.OUT_OF_SCOPE
            elif any(item.origin.value == "seed" for item in matches):
                role = RuleTestCorpusRole.DOCUMENTED_SEED_UNCONFIRMED
            else:
                role = RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED
            assigned_role = role
            available = await bytes_available(sample_sha256)
            if not available:
                record_missing_bytes(sample_sha256, assigned_role)
                role = RuleTestCorpusRole.BYTES_MISSING
            cases[(role, sample_sha256)] = _rule_case(
                investigation_id,
                role,
                sample_sha256,
                format_magic=sample_formats.get(sample_sha256, "unknown"),
                declared_role=assigned_role if role is RuleTestCorpusRole.BYTES_MISSING else None,
                expected_match=_expected_match(assigned_role),
            )

        role_by_hash: dict[str, set[RuleTestCorpusRole]] = {}
        for item in all_manual_imports:
            if _unique_valid_hashes((item.sample_sha256,)):
                role_by_hash.setdefault(item.sample_sha256, set()).add(item.role)
        for item in manual_imports:
            if not _unique_valid_hashes((item.sample_sha256,)):
                continue
            role = item.role
            association_is_confirmed = any(
                association.qualification is SampleQualification.CONFIRMED
                for association in by_hash.get(item.sample_sha256, ())
            )
            if role is RuleTestCorpusRole.HUMAN_CONFIRMED and not association_is_confirmed:
                role = RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED
                add_limitation(
                    f"Le hash {item.sample_sha256} n'a pas de qualification humaine confirmée; "
                    "son rôle positif a été rétrogradé en nouveau candidat non confirmé.",
                    blocking=True,
                )
            elif (
                role
                in {
                    RuleTestCorpusRole.DOCUMENTED_SEED_UNCONFIRMED,
                    RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED,
                }
                and association_is_confirmed
            ):
                role = RuleTestCorpusRole.HUMAN_CONFIRMED
            role_by_hash.setdefault(item.sample_sha256, set()).add(role)
            assigned_role = role
            available = await bytes_available(item.sample_sha256)
            if not available:
                record_missing_bytes(item.sample_sha256, assigned_role)
                role = RuleTestCorpusRole.BYTES_MISSING
            # Recompute the actual type from bytes, even when an older import journal
            # contains a caller supplied or legacy format label.
            format_magic = sample_formats.get(item.sample_sha256, item.format_magic)
            cases[(role, item.sample_sha256)] = RuleTestCase(
                id=uuid5(_UUID_NAMESPACE, f"{investigation_id}:{role.value}:{item.sample_sha256}"),
                sample_sha256=item.sample_sha256,
                role=role,
                provenance=item.provenance,
                expected_match=_expected_match(
                    item.role if role is RuleTestCorpusRole.BYTES_MISSING else role
                ),
                format_magic=format_magic,
                holdout_designated_at=(
                    item.designated_at
                    if item.role
                    in {RuleTestCorpusRole.HOLDOUT_POSITIVE, RuleTestCorpusRole.HOLDOUT_NEGATIVE}
                    else None
                ),
                declared_role=assigned_role if role is RuleTestCorpusRole.BYTES_MISSING else None,
            )

        holdout_roles = {
            RuleTestCorpusRole.HOLDOUT_POSITIVE,
            RuleTestCorpusRole.HOLDOUT_NEGATIVE,
        }
        holdout_hashes = {
            case.sample_sha256
            for case in cases.values()
            if case.role in holdout_roles or case.declared_role in holdout_roles
        }
        imported_holdout_hashes = {
            item.sample_sha256 for item in manual_imports if item.role in holdout_roles
        }
        development_hashes = (
            {
                sample_sha256
                for sample_sha256 in by_hash
                if sample_sha256 not in imported_holdout_hashes
            }
            | {
                case.sample_sha256
                for case in cases.values()
                if case.role not in holdout_roles and case.declared_role not in holdout_roles
            }
            | {item.sample_sha256 for item in manual_imports if item.role not in holdout_roles}
        )
        overlapping_holdout_hashes = tuple(sorted(development_hashes & holdout_hashes))
        if overlapping_holdout_hashes:
            add_limitation("Holdout et développement se chevauchent par SHA-256.", blocking=True)
        roles_by_hash: dict[str, set[RuleTestCorpusRole]] = {
            sample_sha256: set(roles) for sample_sha256, roles in role_by_hash.items()
        }
        for case in cases.values():
            declared_role = case.declared_role
            roles_by_hash.setdefault(case.sample_sha256, set()).add(declared_role or case.role)
        conflicting_role_hashes = tuple(
            sorted(
                sample_sha256 for sample_sha256, roles in roles_by_hash.items() if len(roles) > 1
            )
        )
        if conflicting_role_hashes:
            add_limitation("Un SHA-256 est déclaré avec plusieurs rôles de corpus.", blocking=True)

        try:
            legacy_holdout_hashes = await self.corpus_inventory.holdout_sample_sha256s(
                investigation_id
            )
        except Exception:
            legacy_holdout_hashes = ()
            add_limitation(
                "L'ancien fournisseur de hash holdout n'a pas pu être lu.", blocking=True
            )
        if legacy_holdout_hashes:
            add_limitation(
                "Des hashes holdout sans rôle ni horodatage ont été ignorés; réimportez-les "
                "avec holdout_positive ou holdout_negative avant de créer la règle.",
                blocking=False,
            )

        baseline_reader = getattr(self.corpus_inventory, "goodware_baseline_id", None)
        goodware_baseline_read_failed = False
        try:
            goodware_baseline_id = (
                await baseline_reader(investigation_id) if callable(baseline_reader) else None
            )
        except Exception:
            goodware_baseline_id = None
            goodware_baseline_read_failed = True
            add_limitation(
                "La baseline goodware liée à l'investigation n'a pas pu être lue.", blocking=True
            )
        manual_goodware_present = any(
            item.role is RuleTestCorpusRole.GOODWARE for item in all_manual_imports
        )
        if not manual_goodware_present and not manual_import_read_failed:
            add_limitation(
                "Aucun fichier binaire goodware n'est présent dans le corpus de règles.",
                blocking=False,
            )
        if goodware_baseline_id is None and not goodware_baseline_read_failed:
            add_limitation(
                "Aucune goodware_baseline n'est liée à cette investigation; les tests goodware "
                "restent limités aux imports par SHA.",
                blocking=False,
            )
        ordered_cases = tuple(
            cases[key] for key in sorted(cases, key=lambda item: (item[0].value, item[1]))
        )
        coverage = (
            "Chaque fichier est classé par SHA-256, rôle opérateur persisté, qualification et "
            "format déterminé par magic bytes. Les rôles holdout portent une date de désignation "
            "antérieure à la règle et doivent être disjoints du développement. Les résultats "
            "sur seeds/candidats non confirmés sont des couvertures, jamais des TP/FN."
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
            blocking_limitations=tuple(dict.fromkeys(blocking_limitations)),
            measurement_limitations=tuple(dict.fromkeys(measurement_limitations)),
            holdout_overlap_sha256s=overlapping_holdout_hashes,
            conflicting_role_sha256s=conflicting_role_hashes,
            goodware_baseline_id=goodware_baseline_id,
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
        payload = _report_payload(report, corpus, candidate_status, version=version)
        signature = _signature_manifest(version)
        signature_limitations = _signature_limitations(signature)
        payload["signature"] = signature
        payload["signature_limitations"] = signature_limitations
        payload["limitations"] = list(
            dict.fromkeys((*payload["limitations"], *signature_limitations))
        )
        effective_status, effective_reason = _effective_validation_quality(report, signature)
        final_status, validation_reasons = _one_rule_final_status(report, corpus, version)
        if (
            final_status is OneRuleTestStatus.NOT_VALIDATED
            and effective_status is RuleValidationStatus.REAL_VALIDATED
        ):
            effective_status = RuleValidationStatus.REAL_PARTIAL
            effective_reason = RuleValidationReason.EXECUTION_INCOMPLETE
        payload["validation_status"] = effective_status.value
        payload["validation_reason"] = effective_reason.value
        payload["final_status"] = final_status.value
        payload["validation_reasons"] = list(validation_reasons)
        payload["rule_version_id"] = str(version.id) if version is not None else None
        payload["candidate_status"] = candidate_status.value
        payload["reproduction"] = {
            "tool": "rule.test",
            "command": (
                f'rule.test(candidate_id="{candidate_id}", '
                f"version={version.version if version is not None else 'latest'})"
            ),
            "arguments": {
                "candidate_id": str(candidate_id),
                "version": version.version if version is not None else None,
            },
            "measurement_method": report.measurement_method.value,
            "engine_version": report.engine_version[:200],
            "source_sha256": report.source_sha256,
            "corpus_sha256": report.corpus_sha256,
        }
        observation_payload = {
            "schema": "investigation-yara-test.v1",
            "operation": operation,
            "candidate_id": str(candidate_id),
            "version_id": version_ref,
            "proposal_source_sha256": proposal_sha256 or report.source_sha256,
            **payload,
        }
        observation_payload["summary"] = _bounded_report_payload_summary(observation_payload)
        report_fingerprint = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(observation_payload)
        ).hexdigest()
        idempotency_key = (
            f"{context.idempotency_key}:rule:{operation}:"
            f"{candidate_id}:{version_ref}:{report_fingerprint}"
        )
        if self.artifact_store is not None:
            blob_id, digest = await self.artifact_store.put_canonical_json(
                observation_payload, bucket="investigation-evidence"
            )
            observation = await self.recorder.record_existing_blob(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.YARA_MATCH,
                blob_id=blob_id,
                sha256=digest,
                source="AutoWork RuleWorkbench",
                source_version="investigation-rule-tools.v1",
                object_type="detection_candidate_version",
                object_id=version_ref,
                limitations=tuple(payload["limitations"]),
                idempotency_key=idempotency_key,
            )
        else:
            observation = await self.recorder.record_json(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.YARA_MATCH,
                payload=observation_payload,
                source="AutoWork RuleWorkbench",
                source_version="investigation-rule-tools.v1",
                object_type="detection_candidate_version",
                object_id=version_ref,
                limitations=tuple(payload["limitations"]),
                idempotency_key=idempotency_key,
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
        signature_a = _signature_manifest(version_a)
        signature_b = _signature_manifest(version_b)
        limitations = tuple(
            dict.fromkeys(
                (
                    *corpus.limitations,
                    *_signature_limitations(signature_a),
                    *_signature_limitations(signature_b),
                )
            )
        )
        comparison_payload = {
            "schema": "investigation-yara-comparison.v1",
            "candidate_id": str(version_a.candidate_id),
            "version_a_id": str(version_a.id),
            "version_b_id": str(version_b.id),
            "source_sha256_a": version_a.source_sha256,
            "source_sha256_b": version_b.source_sha256,
            "signature_a": signature_a,
            "signature_b": signature_b,
            "signature_limitations_a": _signature_limitations(signature_a),
            "signature_limitations_b": _signature_limitations(signature_b),
            "engine_version_a": comparison.engine_version_a,
            "engine_version_b": comparison.engine_version_b,
            "metrics_a": comparison.metrics_a,
            "metrics_b": comparison.metrics_b,
            "evaluation_protocol": {
                "development_roles": [
                    RuleTestCorpusRole.CONFIRMED_POSITIVE.value,
                    RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE.value,
                    RuleTestCorpusRole.GOODWARE.value,
                ],
                "holdout_role": RuleTestCorpusRole.HOLDOUT.value,
                "holdout_policy": "Report separately; do not tune against holdout outcomes.",
                "revision_gate": (
                    "rule.revise requires a measured false negative on a confirmed positive; "
                    "candidate and holdout matches do not justify revision."
                ),
                "match_interpretation": (
                    "A YARA match is a byte-pattern hit only; it does not confirm family, "
                    "intent, or behavior."
                ),
                "statistics_interpretation": (
                    "Sensitivity and goodware false-positive rate use real, measured bytes; "
                    "intervals are two-sided 95% Wilson score intervals. A 0/n observation "
                    "does not establish a zero population rate."
                ),
            },
            "reproduction": {
                "tool": "rule.compare",
                "command": (
                    f'rule.compare(candidate_id="{version_a.candidate_id}", '
                    f"version_a={version_a.version}, version_b={version_b.version})"
                ),
                "arguments": {
                    "candidate_id": str(version_a.candidate_id),
                    "version_a": version_a.version,
                    "version_b": version_b.version,
                },
                "measurement_method_a": comparison.measurement_method_a.value,
                "measurement_method_b": comparison.measurement_method_b.value,
                "engine_version_a": comparison.engine_version_a,
                "engine_version_b": comparison.engine_version_b,
                "corpus_sha256": _corpus_manifest_sha256(corpus.cases),
            },
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
            "limitations": list(limitations),
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
            limitations=limitations,
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
    if any(role.non_measurable or role.errors for role in report.roles):
        return DetectionCandidateStatus.CANDIDATE
    goodware_has_errors = bool(goodware.non_measurable or goodware.errors)
    if (
        len(positive.matches) == 1
        and not positive.non_matches
        and not positive.non_measurable
        and not positive.errors
        and not goodware_has_errors
        and not corpus.blocking_limitations
    ):
        return DetectionCandidateStatus.EXPERIMENTAL
    if (
        len(positive.matches) > 1
        and not positive.non_matches
        and not positive.non_measurable
        and not positive.errors
        and not goodware_has_errors
        and not corpus.blocking_limitations
    ):
        return DetectionCandidateStatus.TESTED
    return DetectionCandidateStatus.CANDIDATE


def _corpus_with_measurement_blockers(
    corpus: _RuleCorpusSnapshot, report: RuleTestReport
) -> _RuleCorpusSnapshot:
    if report.execution_status is YaraExecutionStatus.COMPILE_ERROR:
        return corpus

    cases_by_role_hash: dict[tuple[RuleTestCorpusRole, str], RuleTestCase] = {}
    for corpus_case in corpus.cases:
        cases_by_role_hash[(corpus_case.role, corpus_case.sample_sha256)] = corpus_case
        if corpus_case.declared_role is not None:
            cases_by_role_hash[(corpus_case.declared_role, corpus_case.sample_sha256)] = corpus_case
    blockers: list[str] = []
    for role in report.roles:
        for sample_sha256 in role.non_measurable:
            matched_case = cases_by_role_hash.get((role.role, sample_sha256))
            role_name = (
                matched_case.declared_role.value
                if matched_case is not None and matched_case.declared_role is not None
                else role.role.value
            )
            blockers.append(
                f"Le fichier {sample_sha256} du rôle {role_name} n'a pas pu être mesuré; "
                "cette limite bloque le statut testé."
            )
        for error in role.errors:
            matched_case = (
                cases_by_role_hash.get((role.role, error.sample_sha256))
                if error.sample_sha256 is not None
                else None
            )
            role_name = (
                matched_case.declared_role.value
                if matched_case is not None and matched_case.declared_role is not None
                else role.role.value
            )
            sample_detail = (
                f" pour le hash {error.sample_sha256}" if error.sample_sha256 is not None else ""
            )
            blockers.append(
                f"Une erreur de mesure dans le rôle {role_name}{sample_detail} bloque le "
                "statut testé."
            )

    unique_blockers = tuple(dict.fromkeys(blockers))
    if not unique_blockers:
        return corpus
    return replace(
        corpus,
        limitations=tuple(dict.fromkeys((*corpus.limitations, *unique_blockers))),
        blocking_limitations=tuple(dict.fromkeys((*corpus.blocking_limitations, *unique_blockers))),
    )


def _status_for_report(report: RuleTestReport, corpus: _RuleCorpusSnapshot) -> ToolInvocationStatus:
    if report.execution_status.value == "compile_error":
        return ToolInvocationStatus.SUCCEEDED
    if report.execution_status.value != "ok":
        return ToolInvocationStatus.UNAVAILABLE
    measurable = sum(len(role.matches) + len(role.non_matches) for role in report.roles)
    if not corpus.cases or measurable == 0:
        return ToolInvocationStatus.UNAVAILABLE
    if corpus.blocking_limitations or any(
        role.non_measurable or role.errors for role in report.roles
    ):
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
    *,
    version: DetectionCandidateVersion | None = None,
) -> dict[str, Any]:
    revision_recommendation, revision_guidance = _revision_guidance(report)
    positive_role = _role_for(report, RuleTestCorpusRole.CONFIRMED_POSITIVE)
    goodware_role = _role_for(report, RuleTestCorpusRole.GOODWARE)
    false_positive_real_count = sum(
        case.provenance.value == "real" and case.sample_sha256 in goodware_role.matches
        for case in goodware_role.test_cases
    )
    false_negative_real_count = sum(
        case.provenance.value == "real" and case.sample_sha256 in positive_role.non_matches
        for case in positive_role.test_cases
    )
    return {
        "source_sha256": report.source_sha256,
        "execution_status": report.execution_status.value,
        "validation_status": report.validation_status.value,
        "validation_reason": rule_validation_reason(report).value,
        "engine_version": report.engine_version[:200],
        "measurement_method": report.measurement_method.value,
        "evaluation_protocol": {
            "development_roles": [
                RuleTestCorpusRole.HUMAN_CONFIRMED.value,
                RuleTestCorpusRole.DOCUMENTED_SEED_UNCONFIRMED.value,
                RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED.value,
                RuleTestCorpusRole.HARD_NEGATIVE.value,
                RuleTestCorpusRole.GOODWARE.value,
            ],
            "holdout_roles": [
                RuleTestCorpusRole.HOLDOUT_POSITIVE.value,
                RuleTestCorpusRole.HOLDOUT_NEGATIVE.value,
            ],
            "holdout_policy": (
                "Holdout SHA and role are persisted before the rule; "
                "do not tune against holdout outcomes."
            ),
            "revision_gate": (
                "rule.revise requires a measured false negative on a confirmed positive; "
                "candidate and holdout matches do not justify revision."
            ),
            "match_interpretation": (
                "A YARA match is a byte-pattern hit only; it does not confirm family, intent, "
                "or behavior."
            ),
            "statistics_interpretation": (
                "Sensitivity and goodware false-positive rate use real, measured bytes; "
                "intervals are two-sided 95% Wilson score intervals. A 0/n observation "
                "does not establish a zero population rate."
            ),
        },
        "metrics": report.metrics,
        "candidate_status": candidate_status.value,
        "rule_source_sha256": report.source_sha256,
        "test_refs": [str(case.id) for role in report.roles for case in role.test_cases],
        "selection_bias": corpus.selection_bias,
        "coverage_statement": report.coverage_statement,
        "corpus_sha256": report.corpus_sha256,
        "rule_version_id": str(version.id) if version is not None else None,
        "rule_version": version.version_label if version is not None else None,
        "goodware_baseline_id": (
            str(corpus.goodware_baseline_id) if corpus.goodware_baseline_id else None
        ),
        "holdout_overlap_sha256s": list(corpus.holdout_overlap_sha256s),
        "conflicting_role_sha256s": list(corpus.conflicting_role_sha256s),
        "provenance_counts_by_role": report.provenance_counts_by_role,
        "false_positive_count": len(report.false_positive_sha256s),
        "false_positive_real_count": false_positive_real_count,
        "false_positive_sha256s": list(report.false_positive_sha256s),
        "false_negative_count": len(report.false_negative_sha256s),
        "false_negative_real_count": false_negative_real_count,
        "false_negative_sha256s": list(report.false_negative_sha256s),
        "non_measurable_count": sum(len(role.non_measurable) for role in report.roles),
        "revision_recommendation": revision_recommendation,
        "revision_guidance": revision_guidance,
        "corpus_sha256s_by_role": {
            role.value: list(hashes) for role, hashes in corpus.hashes_by_role().items()
        },
        "roles": [
            {
                "role": role.role.value,
                **_qualified_role_counts(role),
                "corpus_sha256s": list(role.corpus_sha256s),
                "provenance_counts": role.provenance_counts,
                "test_cases": [
                    {
                        "sha256": case.sample_sha256,
                        "test_ref": str(case.id),
                        "provenance": case.provenance.value,
                        "format_magic": case.format_magic,
                        "declared_role": case.declared_role.value if case.declared_role else None,
                        "holdout_designated_at": (
                            case.holdout_designated_at.isoformat()
                            if case.holdout_designated_at
                            else None
                        ),
                        "expected_match": _expected_match_for_case(case),
                        "outcome": _case_outcome(role, case.sample_sha256),
                        "family_confirmation": "not_assessed",
                    }
                    for case in role.test_cases
                ],
                "matches": list(role.matches),
                "non_matches": list(role.non_matches),
                "non_measurable": list(role.non_measurable),
                "coverage_matrix": [
                    {
                        "sha256": file_result.sample_sha256,
                        "role": file_result.role.value,
                        "match": file_result.matched,
                        "format_magic": file_result.format_magic,
                        "strings": [
                            {"identifier": hit.identifier, "offsets": list(hit.offsets)}
                            for hit in file_result.strings
                        ],
                        "details_complete": file_result.details_complete,
                        "unavailable_strings": list(file_result.unavailable_strings),
                    }
                    for file_result in role.file_results
                ],
                "errors": [
                    {
                        "code": error.code.value,
                        "message": error.message,
                        "sample_sha256": error.sample_sha256,
                        "line": error.line,
                        "column": error.column,
                        "role": role.role.value,
                    }
                    for error in role.errors
                ],
            }
            for role in report.roles
        ],
        "limitations": list(corpus.limitations),
    }


def _one_rule_final_status(
    report: RuleTestReport,
    corpus: _RuleCorpusSnapshot,
    version: DetectionCandidateVersion | None,
) -> tuple[OneRuleTestStatus, tuple[str, ...]]:
    """Apply the strict measurement gate separately from legacy partial statuses."""
    reasons: list[str] = []
    if version is None:
        reasons.append("rule_version_missing")
    elif version.source_sha256 != report.source_sha256:
        reasons.append("rule_source_sha256_mismatch")
    if report.execution_status is not YaraExecutionStatus.OK:
        reasons.append("engine_not_ok")
    if any(role.errors for role in report.roles):
        reasons.append("engine_or_corpus_errors")
    if any(role.non_measurable for role in report.roles):
        reasons.append("unmeasurable_files_present")
    if any(case.role is RuleTestCorpusRole.BYTES_MISSING for case in corpus.cases):
        reasons.append("bytes_missing_present")
    if corpus.holdout_overlap_sha256s:
        reasons.append("holdout_overlaps_development")
    if corpus.conflicting_role_sha256s:
        reasons.append("conflicting_roles_for_sha256")
    if corpus.goodware_baseline_id is None:
        reasons.append("investigation_goodware_baseline_unbound")

    by_role = {role.role: role for role in report.roles}
    positive_roles = (
        RuleTestCorpusRole.HUMAN_CONFIRMED,
        RuleTestCorpusRole.HOLDOUT_POSITIVE,
    )
    for role_name in positive_roles:
        role = by_role[role_name]
        if not role.test_cases:
            continue
        if set(role.matches) != {case.sample_sha256 for case in role.test_cases}:
            reasons.append(f"in_scope_positive_not_matched:{role_name.value}")
    if not by_role[RuleTestCorpusRole.HUMAN_CONFIRMED].test_cases:
        reasons.append("qualified_positive_corpus_missing")
    if not by_role[RuleTestCorpusRole.HOLDOUT_POSITIVE].test_cases:
        reasons.append("holdout_positive_corpus_missing")

    negative_roles = (
        RuleTestCorpusRole.GOODWARE,
        RuleTestCorpusRole.HARD_NEGATIVE,
        RuleTestCorpusRole.HOLDOUT_NEGATIVE,
    )
    for role_name in negative_roles:
        role = by_role[role_name]
        if role.matches:
            reasons.append(f"negative_match_observed:{role_name.value}")
    if not by_role[RuleTestCorpusRole.GOODWARE].test_cases:
        reasons.append("goodware_corpus_missing")
    if not by_role[RuleTestCorpusRole.HOLDOUT_NEGATIVE].test_cases:
        reasons.append("holdout_negative_corpus_missing")

    holdout_cases = [
        case
        for case in corpus.cases
        if case.role in {RuleTestCorpusRole.HOLDOUT_POSITIVE, RuleTestCorpusRole.HOLDOUT_NEGATIVE}
        or case.declared_role
        in {RuleTestCorpusRole.HOLDOUT_POSITIVE, RuleTestCorpusRole.HOLDOUT_NEGATIVE}
    ]
    for case in holdout_cases:
        if case.holdout_designated_at is None:
            reasons.append("holdout_designation_time_missing")
        elif version is not None and case.holdout_designated_at >= version.created_at:
            reasons.append("holdout_designated_after_rule")
    for role in report.roles:
        for file_result in role.file_results:
            if file_result.matched and not file_result.details_complete:
                reasons.append(f"matched_string_offsets_incomplete:{role.role.value}")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return (
        OneRuleTestStatus.CANDIDATE_MEASURED
        if not unique_reasons
        else OneRuleTestStatus.NOT_VALIDATED,
        unique_reasons,
    )


def _bounded_report_payload_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    role_summary: list[dict[str, int | str]] = []
    test_case_count = 0
    raw_roles = payload.get("roles")
    if isinstance(raw_roles, list):
        for role in raw_roles[: len(RuleTestCorpusRole)]:
            if not isinstance(role, Mapping):
                continue
            cases = role.get("test_cases")
            matches = role.get("matches")
            non_matches = role.get("non_matches")
            non_measurable = role.get("non_measurable")
            coverage = role.get("coverage_matrix")
            errors = role.get("errors")
            case_count = len(cases) if isinstance(cases, list) else 0
            test_case_count += case_count
            role_summary.append(
                {
                    "role": str(role.get("role", "unknown"))[:64],
                    "test_case_count": case_count,
                    "match_count": len(matches) if isinstance(matches, list) else 0,
                    "non_match_count": len(non_matches) if isinstance(non_matches, list) else 0,
                    "non_measurable_count": (
                        len(non_measurable) if isinstance(non_measurable, list) else 0
                    ),
                    "coverage_matrix_count": len(coverage) if isinstance(coverage, list) else 0,
                    "error_count": len(errors) if isinstance(errors, list) else 0,
                }
            )
    raw_reasons = payload.get("validation_reasons")
    raw_limitations = payload.get("limitations")
    return {
        "schema": "investigation-yara-test-summary.v1",
        "source_sha256": payload.get("source_sha256"),
        "corpus_sha256": payload.get("corpus_sha256"),
        "execution_status": payload.get("execution_status"),
        "validation_status": payload.get("validation_status"),
        "validation_reason": payload.get("validation_reason"),
        "final_status": payload.get("final_status"),
        "validation_reasons": [str(item)[:160] for item in raw_reasons[:16]]
        if isinstance(raw_reasons, list)
        else [],
        "false_positive_count": payload.get("false_positive_count", 0),
        "false_negative_count": payload.get("false_negative_count", 0),
        "non_measurable_count": payload.get("non_measurable_count", 0),
        "test_case_count": test_case_count,
        "roles": role_summary,
        "limitations": [str(item)[:400] for item in raw_limitations[:12]]
        if isinstance(raw_limitations, list)
        else [],
    }


def _bounded_report_summary(
    report: Mapping[str, Any],
    *,
    observation_id: UUID,
    report_blob_id: UUID,
    report_sha256: str,
) -> dict[str, Any]:
    bounded = dict(report)
    for key, limit in (
        ("test_refs", 32),
        ("false_positive_sha256s", 32),
        ("false_negative_sha256s", 32),
        ("holdout_overlap_sha256s", 32),
        ("conflicting_role_sha256s", 32),
        ("validation_reasons", 16),
        ("limitations", 12),
    ):
        rows = bounded.get(key)
        if isinstance(rows, list) and len(rows) > limit:
            bounded[f"{key}_total"] = len(rows)
            bounded[f"{key}_omitted_count"] = len(rows) - limit
            bounded[key] = rows[:limit]
    corpus_by_role = bounded.get("corpus_sha256s_by_role")
    if isinstance(corpus_by_role, Mapping):
        bounded["corpus_sha256s_by_role_total"] = {
            str(role): len(values)
            for role, values in corpus_by_role.items()
            if isinstance(values, list)
        }
        bounded["corpus_sha256s_by_role"] = {
            str(role): values[:16]
            for role, values in corpus_by_role.items()
            if isinstance(values, list)
        }
    raw_roles = bounded.get("roles")
    if isinstance(raw_roles, list):
        roles: list[dict[str, Any]] = []
        for raw_role in raw_roles[: len(RuleTestCorpusRole)]:
            if not isinstance(raw_role, Mapping):
                continue
            role = dict(raw_role)
            for key, limit in (
                ("test_cases", 16),
                ("corpus_sha256s", 16),
                ("matches", 32),
                ("non_matches", 32),
                ("non_measurable", 32),
                ("errors", 8),
                ("coverage_matrix", 4),
            ):
                rows = role.get(key)
                if isinstance(rows, list) and len(rows) > limit:
                    role[f"{key}_total"] = len(rows)
                    role[f"{key}_omitted_count"] = len(rows) - limit
                    role[key] = rows[:limit]
            for coverage in role.get("coverage_matrix", ()):
                if not isinstance(coverage, dict):
                    continue
                for key, limit in (("strings", 4), ("unavailable_strings", 4)):
                    rows = coverage.get(key)
                    if isinstance(rows, list) and len(rows) > limit:
                        coverage[f"{key}_total"] = len(rows)
                        coverage[f"{key}_omitted_count"] = len(rows) - limit
                        coverage[key] = rows[:limit]
                for hit in coverage.get("strings", ()):
                    if isinstance(hit, dict) and isinstance(hit.get("offsets"), list):
                        offsets = hit["offsets"]
                        if len(offsets) > 4:
                            hit["offsets_total"] = len(offsets)
                            hit["offsets_omitted_count"] = len(offsets) - 4
                            hit["offsets"] = offsets[:4]
            for error in role.get("errors", ()):
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    error["message"] = error["message"][:300]
            roles.append(role)
        bounded["roles"] = roles
    bounded["test_case_count"] = sum(
        role.get("test_cases_total", len(role.get("test_cases", ())))
        for role in bounded.get("roles", ())
        if isinstance(role, Mapping)
    )
    bounded["complete_report_ref"] = {
        "observation_id": str(observation_id),
        "blob_id": str(report_blob_id),
        "sha256": report_sha256,
        "bucket": "investigation-evidence",
    }
    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_RULE_REPORT_SUMMARY_BYTES:
        return bounded
    return {
        "source_sha256": bounded.get("source_sha256"),
        "rule_source_sha256": bounded.get("rule_source_sha256"),
        "execution_status": bounded.get("execution_status"),
        "validation_status": bounded.get("validation_status"),
        "validation_reason": bounded.get("validation_reason"),
        "final_status": bounded.get("final_status"),
        "validation_reasons": bounded.get("validation_reasons", [])[:16],
        "false_positive_count": bounded.get("false_positive_count", 0),
        "false_negative_count": bounded.get("false_negative_count", 0),
        "test_case_count": bounded.get("test_case_count", 0),
        "complete_report_ref": bounded["complete_report_ref"],
        "summary_truncated": True,
    }


def _report_summary(
    report: RuleTestReport,
    observation_id: UUID | None,
    corpus: _RuleCorpusSnapshot | None,
    version: DetectionCandidateVersion | None = None,
    candidate_status: DetectionCandidateStatus | None = None,
) -> dict[str, Any]:
    revision_recommendation, revision_guidance = _revision_guidance(report)
    positive_role = _role_for(report, RuleTestCorpusRole.CONFIRMED_POSITIVE)
    goodware_role = _role_for(report, RuleTestCorpusRole.GOODWARE)
    signature = _signature_manifest(version)
    effective_status, effective_reason = _effective_validation_quality(report, signature)
    final_status, validation_reasons = _one_rule_final_status(
        report,
        corpus
        or _RuleCorpusSnapshot(
            cases=(),
            selection_bias=report.selection_bias,
            coverage_statement=report.coverage_statement,
            limitations=(),
        ),
        version,
    )
    if (
        final_status is OneRuleTestStatus.NOT_VALIDATED
        and effective_status is RuleValidationStatus.REAL_VALIDATED
    ):
        effective_status = RuleValidationStatus.REAL_PARTIAL
        effective_reason = RuleValidationReason.EXECUTION_INCOMPLETE
    candidate_id = str(version.candidate_id) if version is not None else "unknown"
    version_number = version.version if version is not None else "latest"
    result: dict[str, Any] = {
        "source_sha256": report.source_sha256,
        "rule_source_sha256": report.source_sha256,
        "rule_version_id": str(version.id) if version is not None else None,
        "rule_version": version.version_label if version is not None else None,
        "test_refs": [str(case.id) for role in report.roles for case in role.test_cases],
        "execution_status": report.execution_status.value,
        "validation_status": effective_status.value,
        "validation_reason": effective_reason.value,
        "final_status": final_status.value,
        "validation_reasons": list(validation_reasons),
        "engine_version": report.engine_version[:200],
        "measurement_method": report.measurement_method.value,
        "evaluation_protocol": {
            "development_roles": [
                RuleTestCorpusRole.HUMAN_CONFIRMED.value,
                RuleTestCorpusRole.DOCUMENTED_SEED_UNCONFIRMED.value,
                RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED.value,
                RuleTestCorpusRole.HARD_NEGATIVE.value,
                RuleTestCorpusRole.GOODWARE.value,
            ],
            "holdout_roles": [
                RuleTestCorpusRole.HOLDOUT_POSITIVE.value,
                RuleTestCorpusRole.HOLDOUT_NEGATIVE.value,
            ],
            "holdout_policy": "Report separately; do not tune against holdout outcomes.",
            "revision_gate": (
                "rule.revise requires a measured false negative on a confirmed positive; "
                "candidate and holdout matches do not justify revision."
            ),
            "match_interpretation": (
                "A YARA match is a byte-pattern hit only; it does not confirm family, intent, "
                "or behavior."
            ),
            "statistics_interpretation": (
                "Sensitivity and goodware false-positive rate use real, measured bytes; "
                "intervals are two-sided 95% Wilson score intervals. A 0/n observation "
                "does not establish a zero population rate."
            ),
        },
        "metrics": report.metrics,
        "signature": signature,
        "signature_limitations": _signature_limitations(signature),
        "reproduction": {
            "tool": "rule.test",
            "command": (f'rule.test(candidate_id="{candidate_id}", version={version_number})'),
            "arguments": {
                "candidate_id": str(version.candidate_id) if version is not None else None,
                "version": version.version if version is not None else None,
            },
            "measurement_method": report.measurement_method.value,
            "engine_version": report.engine_version[:200],
            "source_sha256": report.source_sha256,
            "corpus_sha256": report.corpus_sha256,
        },
        "corpus_sha256": report.corpus_sha256,
        "coverage_statement": report.coverage_statement,
        "provenance_counts_by_role": report.provenance_counts_by_role,
        "false_positive_count": len(report.false_positive_sha256s),
        "false_positive_real_count": sum(
            case.provenance.value == "real" and case.sample_sha256 in goodware_role.matches
            for case in goodware_role.test_cases
        ),
        "false_positive_sha256s": list(report.false_positive_sha256s),
        "false_negative_count": len(report.false_negative_sha256s),
        "false_negative_real_count": sum(
            case.provenance.value == "real" and case.sample_sha256 in positive_role.non_matches
            for case in positive_role.test_cases
        ),
        "false_negative_sha256s": list(report.false_negative_sha256s),
        "non_measurable_count": sum(len(role.non_measurable) for role in report.roles),
        "revision_recommendation": revision_recommendation,
        "revision_guidance": revision_guidance,
        "corpus_sha256s_by_role": {
            role.role.value: list(role.corpus_sha256s) for role in report.roles
        },
        "roles": [
            {
                "role": role.role.value,
                **_qualified_role_counts(role),
                "corpus_sha256s": list(role.corpus_sha256s),
                "provenance_counts": role.provenance_counts,
                "test_cases": [
                    {
                        "sha256": case.sample_sha256,
                        "test_ref": str(case.id),
                        "provenance": case.provenance.value,
                        "format_magic": case.format_magic,
                        "declared_role": case.declared_role.value if case.declared_role else None,
                        "holdout_designated_at": (
                            case.holdout_designated_at.isoformat()
                            if case.holdout_designated_at
                            else None
                        ),
                        "expected_match": _expected_match_for_case(case),
                        "outcome": _case_outcome(role, case.sample_sha256),
                        "family_confirmation": "not_assessed",
                    }
                    for case in role.test_cases
                ],
                "matches": list(role.matches),
                "non_matches": list(role.non_matches),
                "non_measurable": list(role.non_measurable),
                "coverage_matrix": [
                    {
                        "sha256": file_result.sample_sha256,
                        "role": file_result.role.value,
                        "match": file_result.matched,
                        "format_magic": file_result.format_magic,
                        "strings": [
                            {"identifier": hit.identifier, "offsets": list(hit.offsets)}
                            for hit in file_result.strings
                        ],
                        "details_complete": file_result.details_complete,
                        "unavailable_strings": list(file_result.unavailable_strings),
                    }
                    for file_result in role.file_results
                ],
                "errors": [
                    {
                        "code": error.code.value,
                        "message": error.message,
                        "sample_sha256": error.sample_sha256,
                        "line": error.line,
                        "column": error.column,
                        "role": role.role.value,
                    }
                    for error in role.errors
                ],
                "error_count": len(role.errors),
            }
            for role in report.roles
        ],
    }
    if candidate_status is not None:
        result["candidate_status"] = candidate_status.value
    if observation_id is not None:
        result["observation_ref"] = str(observation_id)
    if corpus is not None:
        result["corpus_sha256"] = report.corpus_sha256
        result["goodware_baseline_id"] = (
            str(corpus.goodware_baseline_id) if corpus.goodware_baseline_id else None
        )
        result["holdout_overlap_sha256s"] = list(corpus.holdout_overlap_sha256s)
        result["conflicting_role_sha256s"] = list(corpus.conflicting_role_sha256s)
        result["limitations"] = list(
            dict.fromkeys(
                (*corpus.limitations, *_signature_limitations(_signature_manifest(version)))
            )
        )
    return result


def _signature_manifest(version: DetectionCandidateVersion | None) -> dict[str, Any]:
    if version is None:
        return {
            "candidate_id": None,
            "version": None,
            "source_sha256": None,
            "family": None,
            "hypothesis": None,
            "dotnet_imports_discussion": None,
            "generic_dotnet_import_count": 0,
            "condition": None,
            "generic_dotnet_specific_motif_verified": False,
            "generic_dotnet_guard": "not_applicable",
            "source_provenance_complete": False,
            "motifs": [],
        }
    meta = _meta_from_source(version.source)
    motifs: list[dict[str, Any]] = []
    for item in version.motif_provenance:
        provenance: dict[str, Any] = {}
        if item.rationale.startswith(_PERSISTED_MOTIF_PREFIX):
            try:
                parsed = json.loads(item.rationale[len(_PERSISTED_MOTIF_PREFIX) :])
                if isinstance(parsed, Mapping):
                    provenance = dict(parsed)
            except (TypeError, ValueError, json.JSONDecodeError):
                provenance = {}
        source_sha256 = provenance.get("sample_sha256")
        offsets = provenance.get("offsets")
        source_method = provenance.get("provenance_kind")
        article_claim_refs = provenance.get("article_claim_refs")
        exact_source = (
            isinstance(source_sha256, str)
            and re.fullmatch(_SHA256_PATTERN, source_sha256) is not None
            and isinstance(offsets, list)
            and bool(offsets)
            and all(
                isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0
                for offset in offsets
            )
            and isinstance(source_method, str)
            and bool(source_method.strip())
        )
        motifs.append(
            {
                "motif_sha256": hashlib.sha256(item.motif.encode("utf-8")).hexdigest(),
                "kind": provenance.get("kind", "unknown"),
                "source_method": source_method if isinstance(source_method, str) else "unknown",
                "source_sha256": source_sha256 if isinstance(source_sha256, str) else None,
                "offsets": offsets if isinstance(offsets, list) else None,
                "observation_refs": [str(reference) for reference in item.observation_refs],
                "article_claim_refs": (
                    article_claim_refs if isinstance(article_claim_refs, list) else []
                ),
                "rationale": provenance.get("rationale", ""),
                "source_bytes_verified": provenance.get("source_bytes_verified") is True,
                "generic_dotnet_import": is_generic_dotnet_import(item.motif),
                "exact_source_available": exact_source,
            }
        )
    source_generic_values = {
        value for value in _yara_string_literals(version.source) if is_generic_dotnet_import(value)
    }
    provenance_generic_values = {
        item.motif for item in version.motif_provenance if is_generic_dotnet_import(item.motif)
    }
    generic_values = source_generic_values | provenance_generic_values
    generic_specific_verified = any(
        not item["generic_dotnet_import"]
        and item["source_bytes_verified"] is True
        and item["kind"] in {"string", "wide", "hex"}
        for item in motifs
    )
    condition = meta.get("workbench_condition")
    generic_guard_passed = not generic_values or (
        condition == RuleConditionStrategy.ALL.value
        and generic_specific_verified
        and bool(meta.get("hypothesis"))
        and bool(meta.get("dotnet_imports_discussion"))
    )
    return {
        "candidate_id": str(version.candidate_id),
        "version_id": str(version.id),
        "version": version.version,
        "source_sha256": version.source_sha256,
        "source_attribution": _source_attribution_from_version(version),
        "family": meta.get("family"),
        "hypothesis": meta.get("hypothesis"),
        "dotnet_imports_discussion": meta.get("dotnet_imports_discussion"),
        "generic_dotnet_import_count": len(generic_values),
        "condition": condition,
        "generic_dotnet_specific_motif_verified": generic_specific_verified,
        "generic_dotnet_guard": (
            "not_applicable"
            if not generic_values
            else "pass"
            if generic_guard_passed
            else "incomplete"
        ),
        "source_provenance_complete": bool(motifs)
        and all(
            item["exact_source_available"] and item["source_method"] != "unknown" for item in motifs
        ),
        "source_bytes_verified": bool(motifs)
        and all(item["source_bytes_verified"] for item in motifs),
        "motifs": motifs,
    }


def _signature_limitations(signature: Mapping[str, Any]) -> tuple[str, ...]:
    limitations: list[str] = []
    if not signature.get("source_provenance_complete"):
        limitations.append(
            "Motif source SHA-256/method/offset provenance is incomplete; family causality "
            "is not established."
        )
    if signature.get("source_provenance_complete") and not signature.get("source_bytes_verified"):
        limitations.append(
            "Motif byte offsets have source references but were not verified against the "
            "quarantined source bytes."
        )
    if not signature.get("family") and not signature.get("hypothesis"):
        limitations.append("No target family or signature hypothesis is recorded.")
    if signature.get("generic_dotnet_import_count", 0) and not signature.get(
        "dotnet_imports_discussion"
    ):
        limitations.append(
            "Common .NET imports/namespaces lack an explicit discussion and remain generic hints."
        )
    if signature.get("generic_dotnet_guard") == "incomplete":
        limitations.append(
            "Common .NET motifs lack an ALL condition with a source-byte-verified specific motif, "
            "hypothesis, and import discussion; this signature remains a candidate."
        )
    return tuple(limitations)


def _effective_validation_quality(
    report: RuleTestReport, signature: Mapping[str, Any]
) -> tuple[RuleValidationStatus, RuleValidationReason]:
    status = report.validation_status
    reason = rule_validation_reason(report)
    if status is RuleValidationStatus.REAL_VALIDATED and _signature_limitations(signature):
        return RuleValidationStatus.REAL_PARTIAL, RuleValidationReason.SAMPLE_BYTES_MISSING
    return status, reason


def _yara_string_literals(source: str) -> tuple[str, ...]:
    sections = re.search(r"(?ms)^\s*strings\s*:\s*(.*?)^\s*condition\s*:", source)
    if sections is None:
        return ()
    values: list[str] = []
    pattern = r'(?m)^\s*\$[A-Za-z0-9_]+\s*=\s*("(?:\\.|[^"\\])*")'
    for match in re.finditer(pattern, sections[1]):
        try:
            value = ast.literal_eval(match.group(1))
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, str):
            values.append(value)
    return tuple(values)


def _revision_guidance(report: RuleTestReport) -> tuple[str, str]:
    measurable = sum(len(role.matches) + len(role.non_matches) for role in report.roles)
    if report.execution_status is not YaraExecutionStatus.OK or measurable == 0:
        return (
            "not_assessable",
            "A revision decision is not measurable from this test; verify corpus bytes and "
            "rerun rule.test.",
        )
    if _confirmed_positive_false_negative_hashes(report):
        return (
            "revise_false_negative",
            "A confirmed-positive byte sample was missed; revise only to address this measured "
            "false negative and preserve the sample provenance in the report.",
        )
    return (
        "none_justified",
        "Aucune révision justifiée : aucun faux négatif n'est démontré dans le corpus contrôlé.",
    )


def _confirmed_positive_false_negative_hashes(report: RuleTestReport) -> frozenset[str]:
    false_negatives = set(report.false_negative_sha256s)
    return frozenset(
        case.sample_sha256
        for role in report.roles
        if role.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
        for case in role.test_cases
        if case.sample_sha256 in false_negatives and case.sample_sha256 in role.non_matches
    )


def _report_demonstrates_false_negative(payload: Mapping[str, Any]) -> bool:
    false_negatives = payload.get("false_negative_sha256s")
    if not isinstance(false_negatives, Sequence) or isinstance(false_negatives, str | bytes):
        return False
    hashes = {item for item in false_negatives if isinstance(item, str)}
    roles = payload.get("roles")
    if not isinstance(roles, Sequence) or isinstance(roles, str | bytes):
        return False
    for role in roles:
        if (
            not isinstance(role, Mapping)
            or role.get("role") != RuleTestCorpusRole.CONFIRMED_POSITIVE.value
        ):
            continue
        cases = role.get("test_cases")
        if not isinstance(cases, Sequence) or isinstance(cases, str | bytes):
            continue
        if any(
            isinstance(case, Mapping)
            and case.get("outcome") == "no_match"
            and case.get("sha256") in hashes
            for case in cases
        ):
            return True
    return False


def _rule_validation_status(payload: Mapping[str, Any]) -> str:
    try:
        return RuleValidationStatus(payload.get("validation_status", "untested")).value
    except (TypeError, ValueError):
        return RuleValidationStatus.UNTESTED.value


def _case_outcome(role: RuleRoleTestReport, sample_sha256: str) -> str:
    if sample_sha256 in role.matches:
        return "match"
    if sample_sha256 in role.non_matches:
        return "no_match"
    if sample_sha256 in role.non_measurable:
        return "non_measurable"
    return "error"


def _qualified_role_counts(role: RuleRoleTestReport) -> dict[str, Any]:
    positive_roles = {
        RuleTestCorpusRole.HUMAN_CONFIRMED,
        RuleTestCorpusRole.HOLDOUT_POSITIVE,
    }
    negative_roles = {
        RuleTestCorpusRole.GOODWARE,
        RuleTestCorpusRole.HARD_NEGATIVE,
        RuleTestCorpusRole.HOLDOUT_NEGATIVE,
    }
    if role.role in positive_roles:
        return {
            "truth_label": "qualified_positive",
            "true_positive_count": len(role.matches),
            "false_negative_count": len(role.non_matches),
            "false_positive_count": None,
            "true_negative_count": None,
        }
    if role.role in negative_roles:
        return {
            "truth_label": "qualified_negative",
            "true_positive_count": None,
            "false_negative_count": None,
            "false_positive_count": len(role.matches),
            "true_negative_count": len(role.non_matches),
        }
    return {
        "truth_label": "unconfirmed_or_out_of_scope",
        "true_positive_count": None,
        "false_negative_count": None,
        "false_positive_count": None,
        "true_negative_count": None,
    }


def _comparison_summary(
    comparison: RuleVersionComparison,
    observation_id: UUID,
    corpus: _RuleCorpusSnapshot,
    version_a: DetectionCandidateVersion,
    version_b: DetectionCandidateVersion,
) -> dict[str, Any]:
    signature_a = _signature_manifest(version_a)
    signature_b = _signature_manifest(version_b)
    limitations = tuple(
        dict.fromkeys(
            (
                *corpus.limitations,
                *_signature_limitations(signature_a),
                *_signature_limitations(signature_b),
            )
        )
    )
    return {
        "version_a_id": str(comparison.version_a_id),
        "version_b_id": str(comparison.version_b_id),
        "version_a": comparison.version_a,
        "version_b": comparison.version_b,
        "source_sha256_a": version_a.source_sha256,
        "source_sha256_b": version_b.source_sha256,
        "signature_a": signature_a,
        "signature_b": signature_b,
        "signature_limitations_a": _signature_limitations(signature_a),
        "signature_limitations_b": _signature_limitations(signature_b),
        "engine_version_a": comparison.engine_version_a,
        "engine_version_b": comparison.engine_version_b,
        "measurement_method_a": comparison.measurement_method_a.value,
        "measurement_method_b": comparison.measurement_method_b.value,
        "corpus_sha256": _corpus_manifest_sha256(corpus.cases),
        "metrics_a": comparison.metrics_a,
        "metrics_b": comparison.metrics_b,
        "evaluation_protocol": {
            "development_roles": [
                RuleTestCorpusRole.CONFIRMED_POSITIVE.value,
                RuleTestCorpusRole.OUT_OF_CLUSTER_CANDIDATE.value,
                RuleTestCorpusRole.GOODWARE.value,
            ],
            "holdout_role": RuleTestCorpusRole.HOLDOUT.value,
            "holdout_policy": "Report separately; do not tune against holdout outcomes.",
            "revision_gate": (
                "rule.revise requires a measured false negative on a confirmed positive; "
                "candidate and holdout matches do not justify revision."
            ),
            "match_interpretation": (
                "A YARA match is a byte-pattern hit only; it does not confirm family, intent, "
                "or behavior."
            ),
            "statistics_interpretation": (
                "Sensitivity and goodware false-positive rate use real, measured bytes; "
                "intervals are two-sided 95% Wilson score intervals. A 0/n observation "
                "does not establish a zero population rate."
            ),
        },
        "reproduction": {
            "tool": "rule.compare",
            "command": (
                f'rule.compare(candidate_id="{version_a.candidate_id}", '
                f"version_a={comparison.version_a[1:]}, version_b={comparison.version_b[1:]})"
            ),
            "arguments": {
                "candidate_id": str(version_a.candidate_id),
                "version_a": comparison.version_a,
                "version_b": comparison.version_b,
            },
            "measurement_method_a": comparison.measurement_method_a.value,
            "measurement_method_b": comparison.measurement_method_b.value,
            "engine_version_a": comparison.engine_version_a,
            "engine_version_b": comparison.engine_version_b,
            "corpus_sha256": _corpus_manifest_sha256(corpus.cases),
        },
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
        "limitations": list(limitations),
        "observation_ref": str(observation_id),
    }


def _version_summary(
    version: DetectionCandidateVersion, reports: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Expose stable validation status/reason fields for rule.get and publication consumers."""
    matching_reports = tuple(
        report
        for report in reports
        if report.get("operation") == "test"
        and report.get("version_id") == str(version.id)
        and report.get("source_sha256") == version.source_sha256
        and report.get("rule_version_id", str(version.id)) == str(version.id)
    )
    latest_report = matching_reports[-1] if matching_reports else None
    try:
        validation_status = RuleValidationStatus(
            latest_report.get("validation_status", "untested") if latest_report else "untested"
        ).value
    except (TypeError, ValueError):
        validation_status = RuleValidationStatus.UNTESTED.value
    fallback_reasons = {
        RuleValidationStatus.UNTESTED.value: RuleValidationReason.EXPLICIT_TEST_REQUIRED.value,
        RuleValidationStatus.SYNTHETIC_ONLY.value: RuleValidationReason.SYNTHETIC_POSITIVES.value,
        RuleValidationStatus.REAL_VALIDATED.value: RuleValidationReason.VALIDATED.value,
    }
    try:
        validation_reason = RuleValidationReason(
            latest_report.get(
                "validation_reason",
                fallback_reasons.get(
                    validation_status, RuleValidationReason.EXECUTION_INCOMPLETE.value
                ),
            )
            if latest_report
            else RuleValidationReason.EXPLICIT_TEST_REQUIRED.value
        ).value
    except (TypeError, ValueError):
        validation_reason = RuleValidationReason.EXECUTION_INCOMPLETE.value
    return {
        "version_id": str(version.id),
        "version": version.version_label,
        "parent_version_id": str(version.parent_version_id) if version.parent_version_id else None,
        "source_sha256": version.source_sha256,
        "status": version.status.value,
        "validation_status": validation_status,
        "validation_reason": validation_reason,
        "final_status": (
            latest_report.get("final_status", OneRuleTestStatus.NOT_VALIDATED.value)
            if latest_report is not None
            else OneRuleTestStatus.NOT_VALIDATED.value
        ),
        "validation_reasons": (
            latest_report.get("validation_reasons", ["strict_status_unavailable"])
            if latest_report is not None
            else ["explicit_rule_test_required"]
        ),
        "signature": _signature_manifest(version),
        "latest_test_report": (
            {
                "validation_status": validation_status,
                "validation_reason": validation_reason,
                "final_status": latest_report.get(
                    "final_status", OneRuleTestStatus.NOT_VALIDATED.value
                ),
                "validation_reasons": latest_report.get("validation_reasons", []),
                "execution_status": latest_report.get("execution_status"),
                "rule_version_id": latest_report.get("rule_version_id", str(version.id)),
                "rule_source_sha256": latest_report.get(
                    "rule_source_sha256", version.source_sha256
                ),
                "corpus_sha256": latest_report.get("corpus_sha256"),
                "corpus_sha256s_by_role": latest_report.get("corpus_sha256s_by_role", {}),
                "provenance_counts_by_role": latest_report.get("provenance_counts_by_role", {}),
                "false_positive_count": latest_report.get("false_positive_count", 0),
                "false_negative_count": latest_report.get("false_negative_count", 0),
                "non_measurable_count": latest_report.get("non_measurable_count", 0),
                "revision_recommendation": latest_report.get("revision_recommendation"),
                "revision_guidance": latest_report.get("revision_guidance"),
            }
            if latest_report is not None
            else None
        ),
        "test_refs": [str(reference) for reference in version.test_refs],
        "motif_count": len(version.motif_provenance),
        "motif_kinds": sorted(
            {_motif_kind_from_provenance(item.rationale) for item in version.motif_provenance}
        ),
        "reports": list(reports),
    }


def _empty_optional_value(value: Any) -> Any | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, Sequence) and not isinstance(value, str | bytes) and not value:
        return None
    return value


def _normalize_meta(value: Any) -> Mapping[str, str | int | bool] | None:
    if value is None or value == {}:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise _ToolFailed("invalid_meta", agent_message=_META_AGENT_ERROR) from exc
    if not isinstance(value, Mapping):
        raise _ToolFailed("invalid_meta", agent_message=_META_AGENT_ERROR)
    if not value:
        return None
    try:
        validate_json_schema(value, _META_OBJECT_SCHEMA)
    except ValueError as exc:
        raise _ToolFailed("invalid_meta", agent_message=_META_AGENT_ERROR) from exc
    return cast(Mapping[str, str | int | bool], value)


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
        values["observation_refs"] = tuple(
            UUID(value) for value in values.get("observation_refs", ())
        )
        values["article_claim_refs"] = tuple(values.get("article_claim_refs", ()))
        if "offsets" in values:
            values["offsets"] = tuple(values["offsets"])
        result.append(RuleMotif(**values))
    return tuple(result)


def _motif_payload(motif: RuleMotif) -> dict[str, Any]:
    """Serialize one typed motif for the existing rule tool contract."""
    payload: dict[str, Any] = {
        "kind": motif.kind.value,
        "value": motif.value,
        "provenance_kind": motif.provenance_kind.value,
        "provenance_id": motif.provenance_id,
        "observation_refs": [str(item) for item in motif.observation_refs],
        "article_claim_refs": list(motif.article_claim_refs),
        "rationale": motif.rationale,
    }
    for name in (
        "sample_sha256",
        "offsets",
        "positive_support",
        "benign_prevalence",
        "goodware_occurrence_count",
    ):
        value = getattr(motif, name)
        if value is not None:
            payload[name] = list(value) if name == "offsets" else value
    return payload


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
                    article_claim_refs=tuple(metadata.get("article_claim_refs", ())),
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


def _serialize_motif(motif: RuleMotif, *, source_bytes_verified: bool = False) -> str:
    payload = {
        "kind": motif.kind.value,
        "provenance_kind": motif.provenance_kind.value,
        "provenance_id": motif.provenance_id,
        "article_claim_refs": list(motif.article_claim_refs),
        "sample_sha256": motif.sample_sha256,
        "offsets": list(motif.offsets) if motif.offsets is not None else None,
        "rationale": motif.rationale,
        "positive_support": motif.positive_support,
        "benign_prevalence": motif.benign_prevalence,
        "goodware_occurrence_count": motif.goodware_occurrence_count,
        "source_bytes_verified": source_bytes_verified,
    }
    return _PERSISTED_MOTIF_PREFIX + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _serialize_motif_with_source(motif: RuleMotif, attribution: Mapping[str, Any]) -> str:
    payload = json.loads(_serialize_motif(motif)[len(_PERSISTED_MOTIF_PREFIX) :])
    payload["source_attribution"] = dict(attribution)
    return _PERSISTED_MOTIF_PREFIX + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _add_source_attribution(rationale: str, attribution: Mapping[str, Any]) -> str:
    if not rationale.startswith(_PERSISTED_MOTIF_PREFIX):
        raise _ToolFailed("source_attribution_unreadable")
    try:
        payload = json.loads(rationale[len(_PERSISTED_MOTIF_PREFIX) :])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _ToolFailed("source_attribution_unreadable") from exc
    if not isinstance(payload, dict):
        raise _ToolFailed("source_attribution_unreadable")
    payload["source_attribution"] = dict(attribution)
    return _PERSISTED_MOTIF_PREFIX + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _source_attribution_from_version(
    version: DetectionCandidateVersion,
) -> Mapping[str, Any] | None:
    for motif in version.motif_provenance:
        rationale = motif.rationale
        if not rationale.startswith(_PERSISTED_MOTIF_PREFIX):
            continue
        try:
            payload = json.loads(rationale[len(_PERSISTED_MOTIF_PREFIX) :])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        attribution = payload.get("source_attribution") if isinstance(payload, Mapping) else None
        if isinstance(attribution, Mapping):
            return attribution
    return None


def _require_syntax_only_fix(parent_source: str, next_source: str) -> None:
    def semantics(
        source: str,
    ) -> tuple[str, tuple[tuple[str, str, tuple[str, ...]], ...], str]:
        without_comments = re.sub(r"/\*.*?\*/|//[^\n]*$", "", source, flags=re.S | re.M)
        rule_match = re.search(r"(?i)\b(?:(?:private|global)\s+)*rule\s+(\w+)", without_comments)
        strings_match = re.search(r"(?is)\bstrings\s*:\s*(.*?)\bcondition\s*:", without_comments)
        condition_match = re.search(r"(?is)\bcondition\s*:\s*(.*?)(?:\}\s*)?$", without_comments)
        if rule_match is None or strings_match is None or condition_match is None:
            raise _ToolFailed("operator_syntax_fix_changes_detection_semantics")
        declarations = tuple(
            (
                str(match.group(1)),
                " ".join(match.group(2)[1:-1].split())
                if match.group(2).startswith("{")
                else str(match.group(2)),
                tuple(str(match.group(3)).casefold().split()),
            )
            for match in re.finditer(
                r'(?s)(\$[A-Za-z_][A-Za-z0-9_]*)\s*=\s*("(?:\\.|[^"\\])*"|\{[^}]*\}|/(?:\\.|[^/\\])+/)\s*([a-zA-Z_\s]*)',
                strings_match.group(1),
            )
        )
        condition = re.sub(r"\s+", "", condition_match.group(1)).casefold()
        return rule_match.group(1), declarations, condition

    if semantics(parent_source) != semantics(next_source):
        raise _ToolFailed("operator_syntax_fix_changes_detection_semantics")


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
    investigation_id: UUID,
    role: RuleTestCorpusRole,
    sample_sha256: str,
    *,
    format_magic: str = "unknown",
    holdout_designated_at: datetime | None = None,
    declared_role: RuleTestCorpusRole | None = None,
    expected_match: bool | None = None,
) -> RuleTestCase:
    case_id = uuid5(_UUID_NAMESPACE, f"{investigation_id}:{role.value}:{sample_sha256}")
    return RuleTestCase(
        id=case_id,
        role=role,
        sample_sha256=sample_sha256,
        expected_match=_expected_match(declared_role or role)
        if expected_match is None
        else expected_match,
        format_magic=format_magic,
        holdout_designated_at=holdout_designated_at,
        declared_role=declared_role,
    )


def _expected_match(role: RuleTestCorpusRole) -> bool | None:
    if role in {
        RuleTestCorpusRole.HUMAN_CONFIRMED,
        RuleTestCorpusRole.HOLDOUT_POSITIVE,
    }:
        return True
    if role in {
        RuleTestCorpusRole.GOODWARE,
        RuleTestCorpusRole.HARD_NEGATIVE,
        RuleTestCorpusRole.HOLDOUT_NEGATIVE,
    }:
        return False
    return None


def _expected_match_for_case(case: RuleTestCase) -> bool | None:
    return case.expected_result


def _unique_valid_hashes(values: Sequence[str]) -> tuple[str, ...]:
    hashes = set()
    for value in values:
        if isinstance(value, str) and re.fullmatch(_SHA256_PATTERN, value):
            hashes.add(value)
    return tuple(sorted(hashes))


def _bridge_measurement_catalog(
    value: object,
) -> dict[tuple[str, str], Mapping[str, Any]] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes) or not value:
        return None
    result: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in value:
        if not isinstance(row, Mapping):
            return None
        measurement_id = row.get("measurement_id")
        sha256 = row.get("sha256")
        sample_sha256 = row.get("sample_sha256")
        offset = row.get("offset")
        length = row.get("length")
        if (
            not isinstance(measurement_id, str)
            or not measurement_id.strip()
            or not isinstance(row.get("alias"), str)
            or re.fullmatch(r"M[1-9][0-9]*", str(row.get("alias"))) is None
            or not isinstance(sha256, str)
            or re.fullmatch(_SHA256_PATTERN, sha256) is None
            or not isinstance(sample_sha256, str)
            or re.fullmatch(_SHA256_PATTERN, sample_sha256) is None
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(length, bool)
            or not isinstance(length, int)
            or length < 1
            or not isinstance(row.get("encoding"), str)
            or not isinstance(row.get("kind"), str)
            or not isinstance(row.get("value"), str)
        ):
            return None
        identity = (measurement_id, sha256)
        if identity in result:
            return None
        result[identity] = row
    return result or None


def _bridge_measurement_bytes(row: Mapping[str, Any] | None) -> bytes | None:
    if row is None:
        return None
    value = row.get("value")
    kind = row.get("kind")
    encoding = row.get("encoding")
    if not isinstance(value, str) or not isinstance(kind, str) or not isinstance(encoding, str):
        return None
    try:
        if kind == "hex" or encoding == "raw":
            compact = "".join(value.split())
            if not compact or len(compact) % 2 or re.fullmatch(r"[0-9a-fA-F]+", compact) is None:
                return None
            measured = bytes.fromhex(compact)
        elif kind == "wide" or encoding in {"wide", "utf16le"}:
            measured = value.encode("utf-16le", errors="strict")
        elif encoding == "ascii":
            measured = value.encode("ascii", errors="strict")
        elif encoding in {"utf8", "utf-8"}:
            measured = value.encode("utf-8", errors="strict")
        else:
            return None
    except (UnicodeEncodeError, ValueError):
        return None
    return measured if len(measured) == row.get("length") else None


def _exact_yara_string_bytes(source: str, identifier: str) -> bytes | None:
    strings_match = re.search(r"(?is)\bstrings\s*:(.*?)\bcondition\s*:", source)
    if strings_match is None:
        return None
    declaration = re.compile(
        rf'(?s){re.escape(identifier)}\s*=\s*(\{{.*?\}}|"(?:\\.|[^"\\])*"|/(?:\\.|[^/\\])*/)([^\r\n]*)'
    )
    matches = list(declaration.finditer(strings_match.group(1)))
    if len(matches) != 1:
        return None
    pattern, raw_modifiers = matches[0].groups()
    modifiers = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", raw_modifiers, flags=re.S)
    modifier_set = set(modifiers.split())
    if pattern.startswith("{"):
        body = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", pattern[1:-1], flags=re.S)
        byte_pairs = body.split()
        if not byte_pairs or any(
            re.fullmatch(r"[0-9a-fA-F]{2}", item) is None for item in byte_pairs
        ):
            return None
        if modifier_set:
            return None
        return bytes.fromhex("".join(byte_pairs))
    if not pattern.startswith('"') or modifier_set - {"ascii", "wide", "fullword"}:
        return None
    decoded = _decode_yara_string_literal(pattern)
    if decoded is None or {"ascii", "wide"}.issubset(modifier_set):
        return None
    text, raw_bytes = decoded
    if "wide" in modifier_set:
        return text.encode("utf-16le", errors="strict")
    return raw_bytes


def _decode_yara_string_literal(literal: str) -> tuple[str, bytes] | None:
    if len(literal) < 2 or literal[0] != '"' or literal[-1] != '"':
        return None
    text: list[str] = []
    index = 1
    while index < len(literal) - 1:
        character = literal[index]
        index += 1
        if character != "\\":
            text.append(character)
            continue
        if index >= len(literal) - 1:
            return None
        escape = literal[index]
        index += 1
        escaped = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}.get(escape)
        if escaped is not None:
            text.append(escaped)
            continue
        if escape == "x" and index + 2 <= len(literal) - 1:
            digits = literal[index : index + 2]
            if re.fullmatch(r"[0-9a-fA-F]{2}", digits) is None:
                return None
            text.append(chr(int(digits, 16)))
            index += 2
            continue
        return None
    decoded = "".join(text)
    try:
        return decoded, decoded.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None


def _safe_rule_test_diagnostic_reason(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return "validation_failed_without_detail"
    if re.search(
        r"(?i)database\s+url|operator\s+payload|password|secret|token|credential", normalized
    ):
        return "validation_detail_redacted"
    normalized = re.sub(r"(?i)(?:postgres(?:ql)?|redis|https?)://\S+", "[redacted]", normalized)
    normalized = re.sub(r"\b[0-9a-f]{64}\b", "[sha256]", normalized)
    return normalized[:240]


def _corpus_manifest_sha256(
    cases: Sequence[RuleTestCase],
) -> str:
    return rule_corpus_sha256(cases)


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return "missing_argument"
    if isinstance(exc, TypeError):
        return "invalid_argument_type"
    if isinstance(exc, ValueError):
        return "invalid_rule_input"
    return "local_rule_operation_failed"


def _bounded_reason(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:96]
    return normalized or "unspecified"


def _bounded_exception_chain(exc: BaseException) -> str:
    names: list[str] = []
    current = exc
    seen: set[int] = set()
    for _ in range(4):
        if id(current) in seen:
            break
        seen.add(id(current))
        names.append(_safe_exception_type_name(current))
        cause = current.__cause__ or current.__context__
        if cause is None:
            break
        current = cause
    return ":".join(names)[:96] or "UnknownError"


def _unexpected_operation_error_code(exc: Exception) -> str:
    """Expose a stable exception category without forwarding exception text to the agent."""
    exception_types = [_safe_exception_type_name(exc)]
    current: BaseException = exc
    root_cause: BaseException = exc
    seen = {id(exc)}
    for _ in range(8):
        cause = current.__cause__ or current.__context__
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        root_cause = cause
        current = cause
    root_type = _safe_exception_type_name(root_cause)
    if root_type != exception_types[0]:
        exception_types.append(root_type)
    return f"local_rule_operation_failed:{':'.join(exception_types)}"


def _safe_exception_type_name(exc: BaseException) -> str:
    exception_type = type(exc).__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,47}", exception_type):
        return "UnknownError"
    return exception_type


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

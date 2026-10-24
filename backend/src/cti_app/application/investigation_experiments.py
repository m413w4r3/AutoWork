"""Operator-led experiment recording and claim verdict projection."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    InvestigationExperimentRepository,
    ToolInvocationRepository,
    VersionConflictError,
)
from cti_app.application.investigation_state import InvestigationStateRepository
from cti_app.application.investigation_tools import (
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.publication_secret_guard import redact_operational_secrets
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.domain.investigation import (
    InvestigationExecutionMode,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.investigation_experiment import (
    EXPERIMENT_TIMELINE_SCHEMA_VERSION,
    EXPERIMENT_TOOL_SCHEMA_VERSION,
    HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
    HYPOTHESIS_ID_PATTERN,
    AnalysisMatrixStatus,
    ExperimentAvailability,
    ExperimentInputRefV1,
    ExperimentInvocationRefV1,
    ExperimentMethodMode,
    ExperimentObservationRefV1,
    ExperimentReferenceErrorCode,
    ExperimentVerdict,
    InvestigationExperimentV1,
    ObservationCoverage,
    ObservationOutcome,
    consolidate_claim_experiments,
    project_claim_matrix_status,
    project_experiment_matrix_status,
)

_EXPERIMENT_NAMESPACE = UUID("f8f1e82e-9025-42c7-a864-ea8fdbe54f3e")
STRATEGY_PROJECTION_SCHEMA_VERSION = "investigation-strategy-projection.v1"
_UUID_SCHEMA = {
    "type": "string",
    "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
}
_SHA256_SCHEMA = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
_SAFE_SOURCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_ARTICLE_CLAIM_ID = re.compile(
    r"^(?:C[1-9][0-9]{0,5}|T[1-9][0-9]{0,5}|"
    r"S[1-9][0-9]{0,5}P[1-9][0-9]{0,5}(?:\.[1-9][0-9]{0,5})?|"
    r"claim:[a-z0-9]+(?:-[a-z0-9]+)*|fixture:C[1-9][0-9]{0,5})$"
)
_SHA256_REFERENCE = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_UUID_REFERENCE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SAFE_EXPERIMENT_INPUT_TYPES = frozenset({"sample_sha256", "capture_sha256"})
EXPERIMENT_RECORD_TOOL = "experiment.record"
EXPERIMENT_LIST_TOOL = "experiment.list"
HYPOTHESIS_DECLARE_TOOL = "hypothesis.declare"


class _ExperimentReferenceError(ValueError):
    def __init__(self, code: ExperimentReferenceErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def _require_current_g2(envelopes: Any, context: ToolExecutionContext) -> Any:
    envelope = context.envelope
    if envelope is None:
        raise PermissionError("g2_approval_required")
    current_envelope = await envelopes.get(context.investigation_id)
    if (
        current_envelope is None
        or current_envelope.version != envelope.version
        or current_envelope.approval_decision_id != envelope.approval_decision_id
    ):
        raise PermissionError("g2_envelope_changed")
    return envelope


class InMemoryInvestigationExperimentRepository:
    """Thread-safe test/local repository with the production CAS semantics."""

    def __init__(self) -> None:
        self._items: dict[UUID, list[InvestigationExperimentV1]] = {}
        self._lock = asyncio.Lock()

    async def version(self, investigation_id: UUID) -> int:
        items = self._items.get(investigation_id, ())
        return items[-1].version if items else 0

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationExperimentV1, ...]:
        return tuple(self._items.get(investigation_id, ()))

    async def append_if_version(
        self, experiment: InvestigationExperimentV1, *, expected_version: int
    ) -> InvestigationExperimentV1:
        async with self._lock:
            items = self._items.setdefault(experiment.investigation_id, [])
            existing = next((item for item in items if item.id == experiment.id), None)
            if existing is not None:
                if existing.identity_sha256 == experiment.identity_sha256:
                    return existing
                raise ValueError("Experiment identity is already used with different content")
            current_version = items[-1].version if items else 0
            if current_version != expected_version or experiment.version != expected_version + 1:
                raise VersionConflictError("Investigation experiment timeline version changed")
            items.append(experiment)
            return experiment


@dataclass(frozen=True, slots=True)
class InvestigationExperimentTimelineV1:
    schema_version: str
    investigation_id: UUID
    stream_version: int
    experiments: tuple[InvestigationExperimentV1, ...]
    claims: tuple[Any, ...]

    def to_payload(self) -> dict[str, Any]:
        superseded_by: dict[UUID, list[UUID]] = {}
        ordered_experiments = sorted(
            self.experiments, key=lambda item: (item.version, str(item.id))
        )
        for experiment in ordered_experiments:
            for superseded_id in experiment.supersedes_experiment_ids:
                superseded_by.setdefault(superseded_id, []).append(experiment.id)

        experiment_payloads = []
        for item in self.experiments:
            payload = item.to_payload()
            payload.setdefault(
                "supersedes_experiment_ids",
                [str(experiment_id) for experiment_id in item.supersedes_experiment_ids],
            )
            payload["superseded_by"] = [
                str(experiment_id) for experiment_id in superseded_by.get(item.id, ())
            ]
            experiment_payloads.append(payload)
        return {
            "schema_version": self.schema_version,
            "investigation_id": str(self.investigation_id),
            "stream_version": self.stream_version,
            "experiments": experiment_payloads,
            "claims": [item.to_payload() for item in self.claims],
        }


@dataclass(frozen=True, slots=True)
class _CanonicalExperimentReferences:
    experiment_ids: frozenset[str]
    observations_by_experiment_id: Mapping[str, Mapping[str, Any]]
    invocations_by_experiment_id: Mapping[str, Mapping[str, Any]]


def _object_value(item: Any, field_name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(field_name)
    return getattr(item, field_name, None)


def _canonical_experiment_references(
    payload: Mapping[str, Any],
    *,
    observations: Sequence[Any],
    invocations: Sequence[Any],
) -> _CanonicalExperimentReferences:
    investigation_id = str(payload.get("investigation_id", ""))
    experiment_ids: set[str] = set()
    canonical_observation_lookup = {
        str(_object_value(observation, "id")): observation
        for observation in observations
        if str(_object_value(observation, "investigation_id")) == investigation_id
        and _is_canonical_uuid_reference(str(_object_value(observation, "id") or ""))
    }
    canonical_invocation_lookup = {
        str(_object_value(invocation, "id")): invocation
        for invocation in invocations
        if str(_object_value(invocation, "investigation_id")) == investigation_id
        and _is_canonical_uuid_reference(str(_object_value(invocation, "id") or ""))
    }
    observations_by_experiment_id: dict[str, dict[str, Any]] = {}
    invocations_by_experiment_id: dict[str, dict[str, Any]] = {}

    for experiment in _bounded_sequence(payload.get("experiments"), limit=10_000):
        if not isinstance(experiment, Mapping):
            continue
        experiment_id = experiment.get("id")
        if (
            experiment.get("investigation_id") == investigation_id
            and isinstance(experiment_id, str)
            and _is_canonical_uuid_reference(experiment_id)
        ):
            experiment_ids.add(experiment_id)
            linked_observations: dict[str, Any] = {}
            for reference in _bounded_sequence(experiment.get("observation_refs"), limit=128):
                if not isinstance(reference, Mapping):
                    continue
                observation_id = reference.get("observation_id")
                sha256 = reference.get("sha256")
                if not isinstance(observation_id, str) or not _is_canonical_uuid_reference(
                    observation_id
                ):
                    continue
                observation = canonical_observation_lookup.get(observation_id)
                if (
                    observation is not None
                    and _object_value(observation, "sha256") == sha256
                    and _is_canonical_sha256_reference(sha256)
                ):
                    linked_observations[observation_id] = observation
            observations_by_experiment_id[experiment_id] = linked_observations

            linked_invocations: dict[str, Any] = {}
            for reference in _bounded_sequence(experiment.get("tool_invocation_refs"), limit=64):
                if not isinstance(reference, Mapping):
                    continue
                invocation_id = reference.get("invocation_id")
                version = reference.get("version")
                if not isinstance(invocation_id, str) or not _is_canonical_uuid_reference(
                    invocation_id
                ):
                    continue
                invocation = canonical_invocation_lookup.get(invocation_id)
                if (
                    invocation is not None
                    and isinstance(version, int)
                    and not isinstance(version, bool)
                    and version == _object_value(invocation, "version")
                    and 1 <= version <= 1_000_000
                ):
                    linked_invocations[invocation_id] = invocation
            invocations_by_experiment_id[experiment_id] = linked_invocations

    return _CanonicalExperimentReferences(
        experiment_ids=frozenset(experiment_ids),
        observations_by_experiment_id=observations_by_experiment_id,
        invocations_by_experiment_id=invocations_by_experiment_id,
    )


def _matching_canonical_tool_invocation(
    experiment: Mapping[str, Any], invocations_by_id: Mapping[str, Any]
) -> Any | None:
    raw_refs = experiment.get("tool_invocation_refs")
    if not isinstance(raw_refs, (tuple, list)) or not raw_refs:
        return None
    for reference in raw_refs[:64]:
        if not isinstance(reference, Mapping):
            continue
        invocation = invocations_by_id.get(str(reference.get("invocation_id", "")))
        reference_version = reference.get("version")
        if (
            invocation is not None
            and isinstance(reference_version, int)
            and not isinstance(reference_version, bool)
            and 1 <= reference_version <= 1_000_000
            and reference_version == _object_value(invocation, "version")
            and experiment.get("tool_name") == _object_value(invocation, "tool_name")
            and experiment.get("tool_schema_version") == _object_value(invocation, "schema_version")
        ):
            return invocation
    return None


def project_safe_investigation_experiment_timeline(
    payload: Mapping[str, Any],
    *,
    strategy: Mapping[str, Any] | None = None,
    observations: Sequence[Any] = (),
    invocations: Sequence[Any] = (),
    hypotheses: Sequence[Any] = (),
) -> dict[str, Any]:
    """Return the operator timeline without raw proof text, bodies, or untrusted refs.

    Without a verified strategy, claim statuses stay inconclusive. Operational-secret
    redaction is an additional guard; it does not detect encoded sample bodies.
    """
    projected = copy.deepcopy(dict(payload))
    canonical = _canonical_experiment_references(
        projected,
        observations=observations,
        invocations=invocations,
    )
    projected["investigation_id"] = _redact_untrusted_identifier(
        projected.get("investigation_id"), _is_canonical_uuid_reference
    )
    strategy_claims = {
        str(item.get("claim_id")): str(item.get("status", "UNKNOWN"))
        for item in (strategy or {}).get("claims", ())
        if isinstance(item, Mapping)
        and isinstance(item.get("claim_id"), str)
        and _SAFE_ARTICLE_CLAIM_ID.fullmatch(item["claim_id"])
    }
    known_hypothesis_ids = {
        str(_object_value(item, "id"))
        for item in hypotheses
        if _is_safe_hypothesis_id(_object_value(item, "id"))
    }
    known_hypothesis_ids.update(
        str(item.get("id"))
        for item in (strategy or {}).get("hypotheses", ())
        if isinstance(item, Mapping) and _is_safe_hypothesis_id(item.get("id"))
    )
    status_projection = {
        "supported": "supported",
        "refuted": "refuted",
        "UNKNOWN": "inconclusive",
    }
    for claim in projected.get("claims", ()):
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or not _SAFE_ARTICLE_CLAIM_ID.fullmatch(claim_id):
            claim["claim_id"] = "[redacted]"
        strategy_status = strategy_claims.get(str(claim.get("claim_id")), "UNKNOWN")
        claim["status"] = status_projection.get(strategy_status, "inconclusive")
        claim["verdict_assertion_labels"] = [
            label
            for verdict in _bounded_sequence(claim.get("verdicts"), limit=16)
            if (label := _verdict_assertion_label(verdict)) is not None
        ]
        claim["experiment_ids"] = _project_identifier_array(
            claim.get("experiment_ids"),
            lambda value: _is_canonical_uuid_reference(value) and value in canonical.experiment_ids,
            limit=256,
        )
        claim["superseded_by"] = _project_identifier_array(
            claim.get("superseded_by"),
            lambda value: _is_canonical_uuid_reference(value) and value in canonical.experiment_ids,
            limit=256,
        )
        safe_scopes = []
        for scope in claim.get("scopes", ()):
            if isinstance(scope, dict):
                scope["scope"] = "[redacted]"
                scope_experiment_id = scope.get("experiment_id")
                linked_observations = canonical.observations_by_experiment_id.get(
                    str(scope_experiment_id), {}
                )
                scope["experiment_id"] = _redact_untrusted_identifier(
                    scope_experiment_id,
                    lambda value: (
                        _is_canonical_uuid_reference(value) and value in canonical.experiment_ids
                    ),
                )
                scope["observation_ids"] = _project_identifier_array(
                    scope.get("observation_ids"),
                    lambda value, allowed=linked_observations: (
                        _is_canonical_uuid_reference(value) and value in allowed
                    ),
                    limit=128,
                )
                scope["superseded_by"] = _project_identifier_array(
                    scope.get("superseded_by"),
                    lambda value: (
                        _is_canonical_uuid_reference(value) and value in canonical.experiment_ids
                    ),
                    limit=256,
                )
                scope["verdict_assertion_label"] = _verdict_assertion_label(scope.get("verdict"))
                safe_scopes.append(scope)
        claim["scopes"] = safe_scopes

    free_text_fields = (
        "prediction",
        "falsification_criterion",
        "method",
        "expected",
        "actual",
        "scope",
        "implication",
    )
    for experiment in projected.get("experiments", ()):
        if not isinstance(experiment, dict):
            continue
        experiment_id = str(experiment.get("id", ""))
        linked_observations = canonical.observations_by_experiment_id.get(experiment_id, {})
        linked_invocations = canonical.invocations_by_experiment_id.get(experiment_id, {})
        tool_match = _matching_canonical_tool_invocation(
            experiment,
            linked_invocations,
        )
        experiment["id"] = _redact_untrusted_identifier(
            experiment.get("id"),
            lambda value: _is_canonical_uuid_reference(value) and value in canonical.experiment_ids,
        )
        experiment["investigation_id"] = _redact_untrusted_identifier(
            experiment.get("investigation_id"),
            lambda value: (
                _is_canonical_uuid_reference(value) and value == projected.get("investigation_id")
            ),
        )
        for field_name in free_text_fields:
            experiment[field_name] = "[redacted]" if experiment.get(field_name) else ""
        experiment["preconditions"] = []
        experiment["limitations"] = []
        for field_name in ("input_type", "tool_name", "tool_version", "tool_schema_version"):
            value = experiment.get(field_name)
            safe_value = (
                field_name == "input_type"
                and isinstance(value, str)
                and value in _SAFE_EXPERIMENT_INPUT_TYPES
            )
            if not safe_value:
                experiment[field_name] = "[redacted]"
        experiment["author"] = (
            experiment["author"] if experiment.get("author") == "operator-led" else "[redacted]"
        )
        experiment["origin"] = (
            experiment["origin"]
            if experiment.get("origin") == EXPERIMENT_RECORD_TOOL
            else "[redacted]"
        )
        raw_claim_ids = experiment.get("claim_ids")
        safe_claim_ids = [
            item
            if isinstance(item, str) and _SAFE_ARTICLE_CLAIM_ID.fullmatch(item)
            else "[redacted]"
            for item in _bounded_sequence(raw_claim_ids, limit=128)
        ]
        if isinstance(raw_claim_ids, (tuple, list)) and len(raw_claim_ids) > 128:
            safe_claim_ids.append("[redacted]")
        experiment["claim_ids"] = safe_claim_ids
        hypothesis_id = experiment.get("hypothesis_id")
        experiment["hypothesis_id"] = (
            hypothesis_id if hypothesis_id in known_hypothesis_ids else "[redacted]"
        )
        # No arbitrary scalars: byte arrays can be encoded as integers or booleans.
        experiment["tool_arguments"] = {}
        experiment["input_refs"] = _safe_experiment_input_references(
            experiment.get("input_refs"), linked_observations
        )
        linked_observation_types_by_sha = _canonical_experiment_observation_types_by_sha(
            linked_observations
        )
        experiment["next_pivots"] = _project_identifier_array(
            experiment.get("next_pivots"),
            lambda value, allowed=linked_observation_types_by_sha: (
                _is_canonical_sha256_reference(value) and value in allowed
            ),
            limit=128,
        )
        experiment["step_ids"] = _project_identifier_array(
            experiment.get("step_ids"),
            lambda value, allowed=linked_invocations: (
                _is_canonical_uuid_reference(value) and value in allowed
            ),
            limit=64,
        )
        experiment["supersedes_experiment_ids"] = _project_identifier_array(
            experiment.get("supersedes_experiment_ids"),
            lambda value: _is_canonical_uuid_reference(value) and value in canonical.experiment_ids,
            limit=64,
        )
        experiment["superseded_by"] = _project_identifier_array(
            experiment.get("superseded_by"),
            lambda value: _is_canonical_uuid_reference(value) and value in canonical.experiment_ids,
            limit=256,
        )
        experiment["tool_invocation_refs"] = _safe_experiment_invocation_references(
            experiment.get("tool_invocation_refs"), linked_invocations
        )
        experiment["observation_refs"] = _safe_experiment_observation_references(
            experiment.get("observation_refs"), linked_observations
        )
        if tool_match is None:
            experiment["tool_name"] = "[redacted]"
            experiment["tool_schema_version"] = "[redacted]"
        else:
            experiment["tool_name"] = tool_match.tool_name
            experiment["tool_schema_version"] = tool_match.schema_version
        # ToolInvocation has no canonical tool-version field to resolve against.
        experiment["tool_version"] = "[redacted]"
        experiment["verdict_assertion_label"] = _verdict_assertion_label(experiment.get("verdict"))

    redacted, _ = redact_operational_secrets(projected)
    return redacted if isinstance(redacted, dict) else projected


def _bounded_sequence(value: Any, *, limit: int) -> tuple[Any, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(value[:limit])


def _is_canonical_uuid_reference(value: Any) -> bool:
    return isinstance(value, str) and _CANONICAL_UUID_REFERENCE.fullmatch(value) is not None


def _is_canonical_sha256_reference(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_REFERENCE.fullmatch(value) is not None


def _is_safe_hypothesis_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(HYPOTHESIS_ID_PATTERN, value) is not None


def _redact_untrusted_identifier(value: Any, predicate: Any) -> Any:
    return value if predicate(value) else "[redacted]"


def _project_identifier_array(value: Any, predicate: Any, *, limit: int) -> list[Any]:
    projected = [
        item if predicate(item) else "[redacted]" for item in _bounded_sequence(value, limit=limit)
    ]
    if isinstance(value, (tuple, list)) and len(value) > limit:
        projected.append("[redacted]")
    return projected


def _safe_experiment_invocation_references(
    value: Any, invocations_by_id: Mapping[str, Any]
) -> list[dict[str, int | str]]:
    result: list[dict[str, int | str]] = []
    for item in _bounded_sequence(value, limit=64):
        if not isinstance(item, Mapping):
            continue
        version = item.get("version")
        invocation_id = item.get("invocation_id")
        invocation = invocations_by_id.get(str(invocation_id))
        resolved = (
            invocation is not None
            and version == _object_value(invocation, "version")
            and isinstance(version, int)
            and not isinstance(version, bool)
            and 1 <= version <= 1_000_000
        )
        safe_invocation_id: str = (
            invocation_id if resolved and isinstance(invocation_id, str) else "[redacted]"
        )
        safe_version: int | str = (
            version
            if resolved and isinstance(version, int) and not isinstance(version, bool)
            else "[redacted]"
        )
        result.append(
            {
                "invocation_id": safe_invocation_id,
                "version": safe_version,
            }
        )
    if isinstance(value, (tuple, list)) and len(value) > 64:
        result.append({"invocation_id": "[redacted]", "version": "[redacted]"})
    return result


def _safe_experiment_observation_references(
    value: Any, observations_by_id: Mapping[str, Any]
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in _bounded_sequence(value, limit=128):
        if not isinstance(item, Mapping):
            continue
        observation_id = item.get("observation_id")
        sha256 = item.get("sha256")
        observation = observations_by_id.get(str(observation_id))
        resolved = (
            observation is not None
            and _object_value(observation, "sha256") == sha256
            and _is_canonical_sha256_reference(sha256)
        )
        safe_observation_id: str = (
            observation_id if resolved and isinstance(observation_id, str) else "[redacted]"
        )
        safe_sha256: str = sha256 if resolved and isinstance(sha256, str) else "[redacted]"
        result.append(
            {
                "observation_id": safe_observation_id,
                "sha256": safe_sha256,
            }
        )
    if isinstance(value, (tuple, list)) and len(value) > 128:
        result.append({"observation_id": "[redacted]", "sha256": "[redacted]"})
    return result


def _verdict_assertion_label(value: Any) -> str | None:
    if value == ExperimentVerdict.REFUTES.value:
        return "Verdict enregistré : refutes (assertion, non conclusion)"
    return None


def _experiment_observation_input_type(observation: Any) -> str | None:
    object_type = _object_value(observation, "object_type")
    if not isinstance(object_type, str):
        return None
    if object_type == "sample":
        return "sample_sha256"
    if object_type in {"capture", "network_capture"}:
        return "capture_sha256"
    return None


def _observations_by_sha256(observations_by_id: Mapping[str, Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for observation in observations_by_id.values():
        sha256 = _object_value(observation, "sha256")
        if _is_canonical_sha256_reference(sha256):
            result.setdefault(sha256, []).append(observation)
    return result


def _canonical_experiment_observation_types_by_sha(
    observations_by_id: Mapping[str, Any],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for sha256, matches in _observations_by_sha256(observations_by_id).items():
        if len(matches) != 1:
            continue
        input_type = _experiment_observation_input_type(matches[0])
        if input_type is not None:
            result[sha256] = input_type
    return result


def _safe_experiment_input_references(
    value: Any, observations_by_id: Mapping[str, Any]
) -> list[dict[str, str | None]]:
    if not isinstance(value, (tuple, list)):
        return []
    result: list[dict[str, str | None]] = []
    observations_by_sha = _observations_by_sha256(observations_by_id)
    for item in value[:64]:
        if not isinstance(item, Mapping):
            continue
        input_type = item.get("input_type")
        input_id = item.get("input_id")
        sha256 = item.get("sha256")
        matching_observations = (
            observations_by_sha.get(input_id, [])
            if isinstance(input_id, str) and _is_canonical_sha256_reference(input_id)
            else []
        )
        canonical_observation = (
            matching_observations[0] if len(matching_observations) == 1 else None
        )
        canonical_sha256 = (
            _object_value(canonical_observation, "sha256")
            if canonical_observation is not None
            else None
        )
        canonical_input_type = (
            _experiment_observation_input_type(canonical_observation)
            if canonical_observation is not None
            else None
        )
        resolved = (
            canonical_observation is not None
            and canonical_input_type is not None
            and isinstance(input_type, str)
            and input_type in _SAFE_EXPERIMENT_INPUT_TYPES
            and input_id == canonical_sha256
            and (sha256 is None or sha256 == canonical_sha256)
            and input_type == canonical_input_type
        )
        safe_input_id: str = input_id if resolved and isinstance(input_id, str) else "[redacted]"
        safe_sha256: str | None = (
            sha256 if resolved and isinstance(sha256, str) else None if resolved else "[redacted]"
        )
        if resolved:
            safe_input_type = canonical_input_type
        elif matching_observations:
            safe_input_type = "[redacted]"
        else:
            safe_input_type = (
                input_type
                if isinstance(input_type, str) and input_type in _SAFE_EXPERIMENT_INPUT_TYPES
                else "[redacted]"
            )
        result.append(
            {
                "input_type": safe_input_type,
                "input_id": safe_input_id,
                "sha256": safe_sha256,
            }
        )
    if len(value) > 64:
        result.append(
            {"input_type": "[redacted]", "input_id": "[redacted]", "sha256": "[redacted]"}
        )
    return result


async def build_investigation_experiment_timeline(
    repository: InvestigationExperimentRepository, investigation_id: UUID
) -> InvestigationExperimentTimelineV1:
    experiments = tuple(
        item
        for item in await repository.list_for_investigation(investigation_id)
        if item.investigation_id == investigation_id
    )
    return InvestigationExperimentTimelineV1(
        schema_version=EXPERIMENT_TIMELINE_SCHEMA_VERSION,
        investigation_id=investigation_id,
        stream_version=await repository.version(investigation_id),
        experiments=experiments,
        claims=consolidate_claim_experiments(experiments),
    )


def build_investigation_strategy_projection(
    *,
    investigation_id: UUID,
    subject_id: UUID,
    production_run_id: UUID,
    synthesis_artifact_id: UUID,
    snapshot: Any | None,
    snapshot_version: int | None,
    hypotheses: tuple[Any, ...],
    experiments: tuple[InvestigationExperimentV1, ...],
    observations: tuple[Any, ...],
    invocations: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """Build an audit-friendly strategy view from already loaded canonical objects.

    Only claim IDs and source references are projected. Claim, observation and
    sample bodies remain in their canonical stores.
    """
    snapshot_sha256 = getattr(snapshot, "synthesis_sha256", None)
    snapshot_id = getattr(snapshot, "synthesis_artifact_id", None)
    snapshot_matches = bool(
        snapshot is not None
        and snapshot_id == synthesis_artifact_id
        and getattr(snapshot, "subject_id", None) == subject_id
        and getattr(snapshot, "production_run_id", None) == production_run_id
        and isinstance(snapshot_version, int)
        and not isinstance(snapshot_version, bool)
        and snapshot_version > 0
        and getattr(snapshot, "synthesis_version", None) == snapshot_version
        and isinstance(snapshot_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256) is not None
    )
    snapshot_claims = tuple(getattr(getattr(snapshot, "article", None), "claims", ()))
    claims_by_id = {
        str(item.id): item
        for item in snapshot_claims
        if isinstance(getattr(item, "id", None), str) and _SAFE_ARTICLE_CLAIM_ID.fullmatch(item.id)
    }
    declared_hypotheses = {
        str(item.id): item
        for item in hypotheses
        if _is_safe_hypothesis_id(getattr(item, "id", None))
    }
    persisted_observations = {
        item.id: item
        for item in observations
        if getattr(item, "investigation_id", None) == investigation_id
        and isinstance(getattr(item, "id", None), UUID)
        and isinstance(getattr(item, "sha256", None), str)
    }
    relevant_experiments = tuple(
        sorted(
            (item for item in experiments if item.investigation_id == investigation_id),
            key=lambda item: (item.version, str(item.id)),
        )
    )
    canonical_invocations = _canonical_experiment_references(
        {
            "investigation_id": str(investigation_id),
            "experiments": [item.to_payload() for item in relevant_experiments],
        },
        observations=(),
        invocations=invocations,
    )
    foreign_experiment_count = sum(
        item.investigation_id != investigation_id for item in experiments
    )
    claim_ids = set(claims_by_id)
    claim_ids.update(
        claim_id
        for item in relevant_experiments
        for claim_id in item.claim_ids
        if _SAFE_ARTICLE_CLAIM_ID.fullmatch(claim_id)
    )
    matrix_claims: list[dict[str, Any]] = []
    for claim_id in sorted(claim_ids):
        claim = claims_by_id.get(claim_id)
        raw_source_refs = tuple(getattr(claim, "source_refs", ())) if claim is not None else ()
        safe_source_refs = bool(raw_source_refs) and all(
            isinstance(reference, str) and _SAFE_SOURCE_REF.fullmatch(reference)
            for reference in raw_source_refs
        )
        source_resolved = snapshot_matches and claim is not None and safe_source_refs
        claim_experiments = tuple(
            item for item in relevant_experiments if claim_id in item.claim_ids
        )
        valid_hypothesis_experiments = tuple(
            item for item in claim_experiments if item.hypothesis_id in declared_hypotheses
        )
        undeclared_links = len(valid_hypothesis_experiments) != len(claim_experiments)
        matrix_status = (
            AnalysisMatrixStatus.UNKNOWN
            if not source_resolved or undeclared_links
            else _project_claim_status(
                valid_hypothesis_experiments,
                claim_id,
                persisted_observations=persisted_observations,
            )
        )
        active_experiments = _active_experiments_for_claim(claim_experiments, claim_id)
        coverage_values = tuple(item.observation_coverage for item in active_experiments)
        if not coverage_values or all(
            coverage is ObservationCoverage.NONE for coverage in coverage_values
        ):
            coverage = ObservationCoverage.NONE
        elif all(coverage is ObservationCoverage.COMPLETE for coverage in coverage_values):
            coverage = ObservationCoverage.COMPLETE
        else:
            coverage = ObservationCoverage.LIMITED
        matrix_claims.append(
            {
                "claim_id": claim_id,
                "source_ref_status": "resolved" if source_resolved else "UNKNOWN",
                "source_refs": list(raw_source_refs) if source_resolved else [],
                "status": matrix_status.value,
                "coverage": coverage.value,
                "experiment_ids": [str(item.id) for item in claim_experiments],
                "attempted_experiments": [
                    {
                        "id": str(item.id),
                        "version": item.version,
                        "hypothesis_id": (
                            item.hypothesis_id
                            if item.hypothesis_id in declared_hypotheses
                            else "[redacted]"
                        ),
                        "hypothesis_declared": item.hypothesis_id in declared_hypotheses,
                        "tool_name": (
                            matched_invocation.tool_name
                            if (
                                matched_invocation := _matching_canonical_tool_invocation(
                                    item.to_payload(),
                                    canonical_invocations.invocations_by_experiment_id.get(
                                        str(item.id), {}
                                    ),
                                )
                            )
                            is not None
                            else "[redacted]"
                        ),
                        "attempt_identity_sha256": hashlib.sha256(
                            canonical_json_bytes(
                                {
                                    "hypothesis_id": item.hypothesis_id,
                                    "tool_name": item.tool_name,
                                    "tool_version": item.tool_version,
                                    "tool_schema_version": item.tool_schema_version,
                                    "method": item.method,
                                    "method_mode": item.method_mode.value,
                                    "scope": item.scope,
                                    "falsification_criterion": item.falsification_criterion,
                                    "input_refs": [
                                        reference.to_payload() for reference in item.input_refs
                                    ],
                                    "tool_arguments": item.tool_arguments,
                                }
                            )
                        ).hexdigest(),
                        "verdict": item.verdict.value,
                        "outcome": item.actual_outcome.value,
                        "coverage": item.observation_coverage.value,
                        "availability": item.availability.value,
                        "matrix_status": (
                            project_experiment_matrix_status(
                                item, persisted_observations=persisted_observations
                            ).value
                            if item.hypothesis_id in declared_hypotheses
                            else AnalysisMatrixStatus.UNKNOWN.value
                        ),
                    }
                    for item in claim_experiments
                ],
            }
        )

    return {
        "schema_version": STRATEGY_PROJECTION_SCHEMA_VERSION,
        "investigation_id": str(investigation_id),
        "snapshot": {
            "artifact_id": str(snapshot_id) if snapshot_id is not None else None,
            "version": snapshot_version if snapshot_matches else None,
            "sha256": snapshot_sha256 if snapshot_matches else None,
            "verified_identity": snapshot_matches,
        },
        "foreign_experiments_ignored": foreign_experiment_count,
        "hypotheses": [
            {
                "id": _redact_untrusted_identifier(item.id, _is_safe_hypothesis_id),
                "status": getattr(item.status, "value", str(item.status)),
            }
            for item in hypotheses
            if getattr(item, "id", None) is not None
        ],
        "claims": matrix_claims,
    }


def _active_experiments_for_claim(
    experiments: tuple[InvestigationExperimentV1, ...], claim_id: str
) -> tuple[InvestigationExperimentV1, ...]:
    superseded = {
        superseded_id for item in experiments for superseded_id in item.supersedes_experiment_ids
    }
    return tuple(
        item for item in experiments if claim_id in item.claim_ids and item.id not in superseded
    )


def _project_claim_status(
    experiments: tuple[InvestigationExperimentV1, ...],
    claim_id: str,
    *,
    persisted_observations: Mapping[UUID, Any],
) -> AnalysisMatrixStatus:
    return project_claim_matrix_status(
        experiments,
        claim_id,
        persisted_observations=persisted_observations,
    )


class OperatorExperimentToolAdapter:
    """Append experiments only from persisted investigation state and evidence."""

    def __init__(
        self,
        *,
        experiments: InvestigationExperimentRepository,
        observations: EvidenceObservationRepository,
        invocations: ToolInvocationRepository,
        states: InvestigationStateRepository,
        envelopes: Any,
        article_snapshots: ArticleSnapshotPort,
    ) -> None:
        self._experiments = experiments
        self._observations = observations
        self._invocations = invocations
        self._states = states
        self._envelopes = envelopes
        self._article_snapshots = article_snapshots

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        try:
            return await self._execute(arguments, context)
        except VersionConflictError:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                error_code="stale_experiment_version",
                message="The experiment timeline changed; reload it before recording.",
            )
        except PermissionError as exc:
            code = (
                str(exc)
                if str(exc) in {"g2_approval_required", "g2_envelope_changed"}
                else "experiment_forbidden"
            )
            return ToolResult(
                ToolInvocationStatus.POLICY_DENIED,
                error_code=code,
                message=(
                    "Experiment recording is not authorized for the current investigation state."
                ),
            )
        except _ExperimentReferenceError as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                output={"error_code": exc.code.value},
                error_code=exc.code.value,
                message=exc.message,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                output={"error_code": "experiment_invalid"},
                error_code="experiment_invalid",
                message=str(exc),
            )

    async def _execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        envelope = await _require_current_g2(self._envelopes, context)

        state = await self._states.get(context.investigation_id)
        if state is None:
            raise _ExperimentReferenceError(
                ExperimentReferenceErrorCode.UNKNOWN_HYPOTHESIS,
                "The hypothesis is not declared for this investigation.",
            )
        hypothesis_id = str(arguments["hypothesis_id"])
        if all(item.id != hypothesis_id for item in state.hypotheses):
            raise _ExperimentReferenceError(
                ExperimentReferenceErrorCode.UNKNOWN_HYPOTHESIS,
                "The hypothesis is not declared for this investigation.",
            )
        claim_ids = tuple(dict.fromkeys(str(item) for item in arguments["claim_ids"]))
        claims = await self._article_snapshots.get_claims(context.investigation_id, claim_ids)
        valid_claim_ids = {item.id for item in claims}
        if not claim_ids or any(item not in valid_claim_ids for item in claim_ids):
            raise _ExperimentReferenceError(
                ExperimentReferenceErrorCode.UNKNOWN_CLAIM,
                "One or more claim IDs are not in this investigation article.",
            )

        observations: dict[UUID, Any] = {}
        observation_refs = tuple(
            ExperimentObservationRefV1(
                observation_id=UUID(str(item["observation_id"])), sha256=str(item["sha256"])
            )
            for item in arguments["observation_refs"]
        )
        for reference in observation_refs:
            observation = await self._observations.get(reference.observation_id)
            if observation is None or observation.investigation_id != context.investigation_id:
                raise _ExperimentReferenceError(
                    ExperimentReferenceErrorCode.OBSERVATION_MISMATCH,
                    "The observation is missing or belongs to another investigation.",
                )
            if observation.sha256 != reference.sha256:
                raise _ExperimentReferenceError(
                    ExperimentReferenceErrorCode.OBSERVATION_MISMATCH,
                    "The observation SHA-256 does not match persisted evidence.",
                )
            observations[reference.observation_id] = observation

        invocation_refs = tuple(
            ExperimentInvocationRefV1(
                invocation_id=UUID(str(item["invocation_id"])), version=int(item["version"])
            )
            for item in arguments["tool_invocation_refs"]
        )
        invocations: dict[UUID, Any] = {}
        for invocation_reference in invocation_refs:
            invocation = await self._invocations.get(invocation_reference.invocation_id)
            if invocation is None or invocation.investigation_id != context.investigation_id:
                raise ValueError("tool invocation does not belong to this investigation")
            if invocation.version != invocation_reference.version:
                raise ValueError("tool invocation version does not match persisted invocation")
            invocations[invocation_reference.invocation_id] = invocation

        for observation in observations.values():
            if (
                observation.invocation_ref is not None
                and observation.invocation_ref not in invocations
            ):
                raise ValueError("observation tool invocation was not cited")
        if invocation_refs and not any(
            item.tool_name == arguments["tool_name"]
            and item.schema_version == arguments["tool_schema_version"]
            for item in invocations.values()
        ):
            raise ValueError("tool name or schema version does not match persisted invocation")

        input_refs = tuple(
            ExperimentInputRefV1(
                input_type=str(item["input_type"]),
                input_id=str(item["input_id"]),
                sha256=str(item["sha256"]) if item.get("sha256") is not None else None,
            )
            for item in arguments["input_refs"]
        )
        experiment = InvestigationExperimentV1(
            id=uuid5(_EXPERIMENT_NAMESPACE, context.idempotency_key),
            investigation_id=context.investigation_id,
            version=int(arguments["expected_version"]) + 1,
            hypothesis_id=hypothesis_id,
            claim_ids=claim_ids,
            working_state_version=state.version,
            g2_envelope_version=envelope.version,
            prediction=str(arguments["prediction"]),
            falsification_criterion=str(arguments["falsification_criterion"]),
            method=str(arguments["method"]),
            method_mode=ExperimentMethodMode(str(arguments["method_mode"])),
            input_type=str(arguments["input_type"]),
            input_refs=input_refs,
            preconditions=tuple(str(item) for item in arguments["preconditions"]),
            availability=ExperimentAvailability(str(arguments["availability"])),
            tool_name=str(arguments["tool_name"]),
            tool_version=str(arguments["tool_version"]),
            tool_schema_version=str(arguments["tool_schema_version"]),
            tool_arguments=arguments["tool_arguments"],
            step_ids=tuple(str(item) for item in arguments["step_ids"]),
            tool_invocation_refs=invocation_refs,
            observation_refs=observation_refs,
            expected=str(arguments["expected"]),
            actual=str(arguments["actual"]),
            actual_outcome=ObservationOutcome(str(arguments["actual_outcome"])),
            observation_coverage=ObservationCoverage(str(arguments["observation_coverage"])),
            started_at=_datetime(arguments["started_at"]),
            completed_at=_datetime(arguments["completed_at"]),
            scope=str(arguments["scope"]),
            limitations=tuple(str(item) for item in arguments["limitations"]),
            verdict=ExperimentVerdict(str(arguments["verdict"])),
            implication=str(arguments["implication"]),
            next_pivots=tuple(str(item) for item in arguments["next_pivots"]),
            author="operator-led",
            origin=EXPERIMENT_RECORD_TOOL,
            supersedes_experiment_ids=tuple(
                UUID(str(item)) for item in arguments.get("supersedes_experiment_ids", ())
            ),
        )
        existing_experiments = await self._experiments.list_for_investigation(
            context.investigation_id
        )
        _validate_supersedes_experiments(
            existing_experiments,
            experiment_id=experiment.id,
            investigation_id=context.investigation_id,
            version=experiment.version,
            supersedes_experiment_ids=experiment.supersedes_experiment_ids,
        )
        saved = await self._experiments.append_if_version(
            experiment, expected_version=int(arguments["expected_version"])
        )
        timeline = await build_investigation_experiment_timeline(
            self._experiments, context.investigation_id
        )
        observation_list = getattr(self._observations, "list_for_investigation", None)
        invocation_list = getattr(self._invocations, "list_for_investigation", None)
        canonical_observations = (
            tuple(await observation_list(context.investigation_id))
            if callable(observation_list)
            else tuple(observations.values())
        )
        canonical_invocations = (
            tuple(await invocation_list(context.investigation_id))
            if callable(invocation_list)
            else tuple(invocations.values())
        )
        safe_timeline = project_safe_investigation_experiment_timeline(
            timeline.to_payload(),
            observations=canonical_observations,
            invocations=canonical_invocations,
            hypotheses=tuple(state.hypotheses),
        )
        claims_by_id = {item["claim_id"]: item for item in safe_timeline["claims"]}
        experiment_projection = next(
            (item for item in safe_timeline["experiments"] if item.get("id") == str(saved.id)),
            None,
        )
        output = {
            "schema_version": saved.schema_version,
            "experiment_id": str(saved.id),
            "version": saved.version,
            "verdict": saved.verdict.value,
            "verdict_assertion_label": _verdict_assertion_label(saved.verdict.value),
            "experiment": experiment_projection,
            "claim_verdicts": [claims_by_id[item] for item in saved.claim_ids],
        }
        safe_output, _ = redact_operational_secrets(output)
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            observation_refs=tuple(
                reference.observation_id for reference in saved.observation_refs
            ),
            output=safe_output,
        )


class _ExperimentListToolAdapter:
    def __init__(
        self,
        experiments: InvestigationExperimentRepository,
        *,
        observations: EvidenceObservationRepository | None = None,
        invocations: ToolInvocationRepository | None = None,
        states: InvestigationStateRepository | None = None,
    ) -> None:
        self._experiments = experiments
        self._observations = observations
        self._invocations = invocations
        self._states = states

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        del arguments
        timeline = await build_investigation_experiment_timeline(
            self._experiments, context.investigation_id
        )
        canonical_observations = (
            tuple(await self._observations.list_for_investigation(context.investigation_id))
            if self._observations is not None
            else ()
        )
        canonical_invocations = (
            tuple(await self._invocations.list_for_investigation(context.investigation_id))
            if self._invocations is not None
            else ()
        )
        state = (
            await self._states.get(context.investigation_id) if self._states is not None else None
        )
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            output=project_safe_investigation_experiment_timeline(
                timeline.to_payload(),
                observations=canonical_observations,
                invocations=canonical_invocations,
                hypotheses=tuple(state.hypotheses) if state is not None else (),
            ),
        )


class OperatorHypothesisDeclareToolAdapter:
    """Confirm a hypothesis that the invocation service appended in its state CAS."""

    def __init__(self, *, states: InvestigationStateRepository, envelopes: Any) -> None:
        self._states = states
        self._envelopes = envelopes

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        try:
            if "operator.write" not in context.available_capabilities:
                return ToolResult(
                    ToolInvocationStatus.POLICY_DENIED,
                    error_code="operator_write_required",
                    message="The operator.write capability is required to declare a hypothesis.",
                )
            await _require_current_g2(self._envelopes, context)
            state = await self._states.get(context.investigation_id)
            hypothesis_id = str(arguments["hypothesis_id"])
            statement = str(arguments["statement"])
            if re.fullmatch(HYPOTHESIS_ID_PATTERN, hypothesis_id) is None:
                raise ValueError("The hypothesis ID format is invalid.")
            if state is None or state.execution_mode is not InvestigationExecutionMode.OPERATOR_LED:
                raise ValueError("An operator-led working state is required.")
            hypothesis = next((item for item in state.hypotheses if item.id == hypothesis_id), None)
            if hypothesis is None:
                raise ValueError("The hypothesis declaration was not included in the state CAS.")
            if hypothesis.statement != statement:
                return ToolResult(
                    ToolInvocationStatus.FAILED,
                    output={"error_code": "hypothesis_identity_conflict"},
                    error_code="hypothesis_identity_conflict",
                    message="This hypothesis ID is already declared with different content.",
                )
            return ToolResult(
                ToolInvocationStatus.SUCCEEDED,
                output={"hypothesis_id": hypothesis.id, "statement": hypothesis.statement},
            )
        except PermissionError as exc:
            code = (
                str(exc)
                if str(exc) in {"g2_approval_required", "g2_envelope_changed"}
                else "hypothesis_declaration_forbidden"
            )
            return ToolResult(
                ToolInvocationStatus.POLICY_DENIED,
                output={"error_code": code},
                error_code=code,
                message="Hypothesis declaration requires the current approved G2 envelope.",
            )
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                output={"error_code": "hypothesis_declaration_invalid"},
                error_code="hypothesis_declaration_invalid",
                message=str(exc),
            )


def register_operator_experiment_tools(
    registry: ToolRegistry,
    *,
    experiments: InvestigationExperimentRepository,
    observations: EvidenceObservationRepository,
    invocations: ToolInvocationRepository,
    states: InvestigationStateRepository,
    envelopes: Any,
    article_snapshots: ArticleSnapshotPort,
) -> None:
    adapter = OperatorExperimentToolAdapter(
        experiments=experiments,
        observations=observations,
        invocations=invocations,
        states=states,
        envelopes=envelopes,
        article_snapshots=article_snapshots,
    )
    registry.register(
        ToolDefinition(
            name=HYPOTHESIS_DECLARE_TOOL,
            schema_version=HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
            input_schema={
                "type": "object",
                "properties": {
                    "hypothesis_id": {
                        "type": "string",
                        "pattern": HYPOTHESIS_ID_PATTERN,
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "statement": {"type": "string", "minLength": 1, "maxLength": 8_000},
                },
                "required": ["hypothesis_id", "statement"],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.CONFIDENTIAL,
            limitations=(
                "Hypotheses are appended to the canonical working state by the operator CAS.",
            ),
            capability_key="operator.write",
        ),
        OperatorHypothesisDeclareToolAdapter(states=states, envelopes=envelopes),
    )
    text = {"type": "string", "minLength": 1, "maxLength": 8_000}
    string_array = {"type": "array", "maxItems": 128, "items": text}
    register_schema = {
        "type": "object",
        "properties": {
            "expected_version": {"type": "integer", "minimum": 0},
            "hypothesis_id": {
                "type": "string",
                "pattern": HYPOTHESIS_ID_PATTERN,
            },
            "claim_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
            },
            "supersedes_experiment_ids": {
                "type": "array",
                "maxItems": 64,
                "uniqueItems": True,
                "items": _UUID_SCHEMA,
                "description": "Earlier experiments in this investigation replaced by this record.",
            },
            "prediction": text,
            "falsification_criterion": text,
            "method": text,
            "method_mode": {"type": "string", "enum": [ExperimentMethodMode.PASSIVE.value]},
            "input_type": {"type": "string", "minLength": 1, "maxLength": 128},
            "input_refs": {
                "type": "array",
                "minItems": 1,
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "properties": {
                        "input_type": {"type": "string", "minLength": 1, "maxLength": 128},
                        "input_id": {"type": "string", "minLength": 1, "maxLength": 512},
                        "sha256": {"type": ["string", "null"], "pattern": r"^[0-9a-f]{64}$"},
                    },
                    "required": ["input_type", "input_id", "sha256"],
                    "additionalProperties": False,
                },
            },
            "preconditions": string_array,
            "availability": {
                "type": "string",
                "enum": [item.value for item in ExperimentAvailability],
            },
            "tool_name": {"type": "string", "minLength": 1, "maxLength": 256},
            "tool_version": {"type": "string", "minLength": 1, "maxLength": 128},
            "tool_schema_version": {"type": "string", "minLength": 1, "maxLength": 128},
            "tool_arguments": {"type": "object", "additionalProperties": True},
            "step_ids": {
                "type": "array",
                "maxItems": 64,
                "items": {"type": "string", "minLength": 1, "maxLength": 256},
            },
            "tool_invocation_refs": {
                "type": "array",
                "maxItems": 64,
                "items": {
                    "type": "object",
                    "properties": {
                        "invocation_id": _UUID_SCHEMA,
                        "version": {"type": "integer", "minimum": 1},
                    },
                    "required": ["invocation_id", "version"],
                    "additionalProperties": False,
                },
            },
            "observation_refs": {
                "type": "array",
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "properties": {"observation_id": _UUID_SCHEMA, "sha256": _SHA256_SCHEMA},
                    "required": ["observation_id", "sha256"],
                    "additionalProperties": False,
                },
            },
            "expected": text,
            "actual": text,
            "actual_outcome": {
                "type": "string",
                "enum": [item.value for item in ObservationOutcome],
            },
            "observation_coverage": {
                "type": "string",
                "enum": [item.value for item in ObservationCoverage],
            },
            "started_at": {"type": "string", "format": "date-time"},
            "completed_at": {"type": "string", "format": "date-time"},
            "scope": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "limitations": string_array,
            "verdict": {"type": "string", "enum": [item.value for item in ExperimentVerdict]},
            "implication": {"type": "string", "maxLength": 8_000},
            "next_pivots": {
                "type": "array",
                "maxItems": 128,
                "items": _SHA256_SCHEMA,
            },
        },
        "required": [
            "expected_version",
            "hypothesis_id",
            "claim_ids",
            "prediction",
            "falsification_criterion",
            "method",
            "method_mode",
            "input_type",
            "input_refs",
            "preconditions",
            "availability",
            "tool_name",
            "tool_version",
            "tool_schema_version",
            "tool_arguments",
            "step_ids",
            "tool_invocation_refs",
            "observation_refs",
            "expected",
            "actual",
            "actual_outcome",
            "observation_coverage",
            "started_at",
            "completed_at",
            "scope",
            "limitations",
            "verdict",
            "implication",
            "next_pivots",
        ],
        "additionalProperties": False,
    }
    registry.register(
        ToolDefinition(
            name=EXPERIMENT_RECORD_TOOL,
            schema_version=EXPERIMENT_TOOL_SCHEMA_VERSION,
            input_schema=register_schema,
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.CONFIDENTIAL,
            limitations=(
                "A record can cite only persisted investigation observations and invocation "
                "versions.",
                "Limited visibility cannot turn not_observed into a refutation.",
            ),
            capability_key="operator.write",
        ),
        adapter,
    )
    registry.register(
        ToolDefinition(
            name=EXPERIMENT_LIST_TOOL,
            schema_version=EXPERIMENT_TIMELINE_SCHEMA_VERSION,
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.CONFIDENTIAL,
            limitations=("The response is a projection of persisted experiment history.",),
            capability_key="investigation.local_read",
        ),
        _ExperimentListToolAdapter(
            experiments,
            observations=observations,
            invocations=invocations,
            states=states,
        ),
    )


def _datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Experiment dates must use ISO 8601 strings")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Experiment dates must use ISO 8601 strings") from exc


def _validate_supersedes_experiments(
    experiments: tuple[InvestigationExperimentV1, ...],
    *,
    experiment_id: UUID,
    investigation_id: UUID,
    version: int,
    supersedes_experiment_ids: tuple[UUID, ...],
) -> None:
    if experiment_id in supersedes_experiment_ids:
        raise _ExperimentReferenceError(
            ExperimentReferenceErrorCode.SUPERSEDED_EXPERIMENT_MISMATCH,
            "An experiment cannot supersede itself.",
        )
    by_id = {item.id: item for item in experiments}
    for superseded_id in supersedes_experiment_ids:
        superseded = by_id.get(superseded_id)
        if (
            superseded is None
            or superseded.investigation_id != investigation_id
            or superseded.version >= version
        ):
            raise _ExperimentReferenceError(
                ExperimentReferenceErrorCode.SUPERSEDED_EXPERIMENT_MISMATCH,
                "Superseded experiments must exist earlier in this investigation.",
            )

    supersedes_by_id = {item.id: item.supersedes_experiment_ids for item in experiments}
    supersedes_by_id[experiment_id] = supersedes_experiment_ids

    def reaches_experiment(current_id: UUID, target_id: UUID, visited: set[UUID]) -> bool:
        if current_id == target_id:
            return True
        if current_id in visited:
            return False
        visited.add(current_id)
        return any(
            reaches_experiment(parent_id, target_id, visited)
            for parent_id in supersedes_by_id.get(current_id, ())
        )

    if any(
        reaches_experiment(superseded_id, experiment_id, set())
        for superseded_id in supersedes_experiment_ids
    ):
        raise _ExperimentReferenceError(
            ExperimentReferenceErrorCode.SUPERSEDED_EXPERIMENT_MISMATCH,
            "Superseding experiment references cannot form a cycle.",
        )


__all__ = [
    "EXPERIMENT_LIST_TOOL",
    "EXPERIMENT_RECORD_TOOL",
    "HYPOTHESIS_DECLARE_TOOL",
    "STRATEGY_PROJECTION_SCHEMA_VERSION",
    "InMemoryInvestigationExperimentRepository",
    "InvestigationExperimentTimelineV1",
    "OperatorExperimentToolAdapter",
    "OperatorHypothesisDeclareToolAdapter",
    "build_investigation_experiment_timeline",
    "build_investigation_strategy_projection",
    "register_operator_experiment_tools",
]

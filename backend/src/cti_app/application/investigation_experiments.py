"""Operator-led experiment recording and claim verdict projection."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
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
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.domain.investigation import InvestigationExecutionMode, ToolInvocationStatus
from cti_app.domain.investigation_experiment import (
    EXPERIMENT_TIMELINE_SCHEMA_VERSION,
    EXPERIMENT_TOOL_SCHEMA_VERSION,
    HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION,
    HYPOTHESIS_ID_PATTERN,
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
)

_EXPERIMENT_NAMESPACE = UUID("f8f1e82e-9025-42c7-a864-ea8fdbe54f3e")
_UUID_SCHEMA = {
    "type": "string",
    "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
}
_SHA256_SCHEMA = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
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


async def build_investigation_experiment_timeline(
    repository: InvestigationExperimentRepository, investigation_id: UUID
) -> InvestigationExperimentTimelineV1:
    experiments = await repository.list_for_investigation(investigation_id)
    return InvestigationExperimentTimelineV1(
        schema_version=EXPERIMENT_TIMELINE_SCHEMA_VERSION,
        investigation_id=investigation_id,
        stream_version=await repository.version(investigation_id),
        experiments=experiments,
        claims=consolidate_claim_experiments(experiments),
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
        claims_by_id = {item.claim_id: item for item in timeline.claims}
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            observation_refs=tuple(
                reference.observation_id for reference in saved.observation_refs
            ),
            output={
                "schema_version": saved.schema_version,
                "experiment_id": str(saved.id),
                "version": saved.version,
                "verdict": saved.verdict.value,
                "claim_verdicts": [claims_by_id[item].to_payload() for item in saved.claim_ids],
            },
        )


class _ExperimentListToolAdapter:
    def __init__(self, experiments: InvestigationExperimentRepository) -> None:
        self._experiments = experiments

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        del arguments
        timeline = await build_investigation_experiment_timeline(
            self._experiments, context.investigation_id
        )
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            output=timeline.to_payload(),
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
        _ExperimentListToolAdapter(experiments),
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
    "InMemoryInvestigationExperimentRepository",
    "InvestigationExperimentTimelineV1",
    "OperatorExperimentToolAdapter",
    "OperatorHypothesisDeclareToolAdapter",
    "build_investigation_experiment_timeline",
    "register_operator_experiment_tools",
]

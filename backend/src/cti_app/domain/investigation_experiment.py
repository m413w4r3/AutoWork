"""Versioned, append-only experiment records for investigation claims."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.investigation import canonical_json_bytes, freeze_json, thaw_json

EXPERIMENT_SCHEMA_VERSION = "investigation-experiment.v1"
EXPERIMENT_TIMELINE_SCHEMA_VERSION = "investigation-experiment-timeline.v1"
EXPERIMENT_TOOL_SCHEMA_VERSION = "operator-experiment.v1"
HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION = "operator-hypothesis-declare.v1"
HYPOTHESIS_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_ARGUMENT_KEY = re.compile(
    r"(?:secret|token|password|passwd|api[_-]?key|authorization|credential|cookie|private[_-]?key)",
    re.IGNORECASE,
)


class ExperimentVerdict(StrEnum):
    SUPPORTS = "supports"
    REFUTES = "refutes"
    QUALIFIES = "qualifies"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"


class ExperimentAvailability(StrEnum):
    AVAILABLE = "available"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"


class ExperimentMethodMode(StrEnum):
    PASSIVE = "passive"


class ObservationOutcome(StrEnum):
    OBSERVED = "observed"
    NOT_OBSERVED = "not_observed"
    UNKNOWN = "unknown"


class ObservationCoverage(StrEnum):
    COMPLETE = "complete"
    LIMITED = "limited"
    NONE = "none"


class ConsolidatedClaimStatus(StrEnum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    QUALIFIED = "qualified"
    INCONCLUSIVE = "inconclusive"


class ExperimentReferenceErrorCode(StrEnum):
    UNKNOWN_HYPOTHESIS = "experiment_hypothesis_unknown"
    UNKNOWN_CLAIM = "experiment_claim_unknown"
    OBSERVATION_MISMATCH = "experiment_observation_ref_mismatch"
    SUPERSEDED_EXPERIMENT_MISMATCH = "superseded_experiment_ref_mismatch"


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _nonempty(value: str, name: str, *, maximum: int = 8_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _validate_safe_arguments(value: Any, path: str = "tool_arguments") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _SENSITIVE_ARGUMENT_KEY.search(key):
                raise ValueError("Tool arguments must not contain secret-bearing keys")
            _validate_safe_arguments(item, f"{path}.{key}")
    elif isinstance(value, (tuple, list)):
        for index, item in enumerate(value):
            _validate_safe_arguments(item, f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value) > 4_096:
            raise ValueError(f"{path} string exceeds 4096 characters")
        if re.search(r"(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=-]{12,}", value):
            raise ValueError("Tool arguments must not contain credential-like values")
    elif value is None or isinstance(value, (bool, int, float)):
        return
    else:
        raise ValueError(f"{path} must contain JSON values")


@dataclass(frozen=True, slots=True)
class ExperimentInputRefV1:
    input_type: str
    input_id: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.input_type, "input_type", maximum=128)
        _nonempty(self.input_id, "input_id", maximum=512)
        if self.sha256 is not None and _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("Input reference sha256 must be lowercase hexadecimal")

    def to_payload(self) -> dict[str, Any]:
        return {"input_type": self.input_type, "input_id": self.input_id, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ExperimentObservationRefV1:
    observation_id: UUID
    sha256: str

    def __post_init__(self) -> None:
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("Observation reference sha256 must be lowercase hexadecimal")

    def to_payload(self) -> dict[str, str]:
        return {"observation_id": str(self.observation_id), "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ExperimentInvocationRefV1:
    invocation_id: UUID
    version: int

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Tool invocation version must be positive")

    def to_payload(self) -> dict[str, Any]:
        return {"invocation_id": str(self.invocation_id), "version": self.version}


@dataclass(frozen=True, slots=True)
class InvestigationExperimentV1:
    id: UUID
    investigation_id: UUID
    version: int
    hypothesis_id: str
    claim_ids: tuple[str, ...]
    working_state_version: int
    g2_envelope_version: int
    prediction: str
    falsification_criterion: str
    method: str
    input_type: str
    input_refs: tuple[ExperimentInputRefV1, ...]
    preconditions: tuple[str, ...]
    availability: ExperimentAvailability
    tool_name: str
    tool_version: str
    tool_schema_version: str
    tool_arguments: Mapping[str, Any]
    step_ids: tuple[str, ...]
    tool_invocation_refs: tuple[ExperimentInvocationRefV1, ...]
    observation_refs: tuple[ExperimentObservationRefV1, ...]
    expected: str
    actual: str
    actual_outcome: ObservationOutcome
    observation_coverage: ObservationCoverage
    started_at: datetime
    completed_at: datetime
    recorded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    scope: str = ""
    limitations: tuple[str, ...] = ()
    verdict: ExperimentVerdict = ExperimentVerdict.INCONCLUSIVE
    implication: str = ""
    next_pivots: tuple[str, ...] = ()
    author: str = "operator-led"
    origin: str = "operator_tool"
    schema_version: str = EXPERIMENT_SCHEMA_VERSION
    method_mode: ExperimentMethodMode = ExperimentMethodMode.PASSIVE
    supersedes_experiment_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != EXPERIMENT_SCHEMA_VERSION:
            raise ValueError("Unsupported investigation experiment schema version")
        if self.version < 1 or self.working_state_version < 1 or self.g2_envelope_version < 1:
            raise ValueError("Experiment and source versions must be positive")
        if not isinstance(self.availability, ExperimentAvailability):
            raise TypeError("availability must use ExperimentAvailability")
        if not isinstance(self.method_mode, ExperimentMethodMode):
            raise TypeError("method_mode must use ExperimentMethodMode")
        if not isinstance(self.actual_outcome, ObservationOutcome):
            raise TypeError("actual_outcome must use ObservationOutcome")
        if not isinstance(self.observation_coverage, ObservationCoverage):
            raise TypeError("observation_coverage must use ObservationCoverage")
        if not isinstance(self.verdict, ExperimentVerdict):
            raise TypeError("verdict must use ExperimentVerdict")
        object.__setattr__(
            self, "hypothesis_id", _nonempty(self.hypothesis_id, "hypothesis_id", maximum=128)
        )
        object.__setattr__(self, "prediction", _nonempty(self.prediction, "prediction"))
        object.__setattr__(
            self,
            "falsification_criterion",
            _nonempty(self.falsification_criterion, "falsification_criterion"),
        )
        object.__setattr__(self, "method", _nonempty(self.method, "method"))
        object.__setattr__(
            self, "input_type", _nonempty(self.input_type, "input_type", maximum=128)
        )
        object.__setattr__(self, "tool_name", _nonempty(self.tool_name, "tool_name", maximum=256))
        object.__setattr__(
            self, "tool_version", _nonempty(self.tool_version, "tool_version", maximum=128)
        )
        object.__setattr__(
            self,
            "tool_schema_version",
            _nonempty(self.tool_schema_version, "tool_schema_version", maximum=128),
        )
        object.__setattr__(self, "expected", _nonempty(self.expected, "expected"))
        object.__setattr__(self, "actual", _nonempty(self.actual, "actual"))
        object.__setattr__(self, "scope", _nonempty(self.scope, "scope", maximum=2_000))
        object.__setattr__(self, "author", _nonempty(self.author, "author", maximum=256))
        object.__setattr__(self, "origin", _nonempty(self.origin, "origin", maximum=256))
        object.__setattr__(self, "claim_ids", tuple(dict.fromkeys(self.claim_ids)))
        supersedes_experiment_ids = tuple(self.supersedes_experiment_ids)
        if len(supersedes_experiment_ids) > 64 or len(set(supersedes_experiment_ids)) != len(
            supersedes_experiment_ids
        ):
            raise ValueError("Superseded experiment references exceed their limits")
        if self.id in supersedes_experiment_ids:
            raise ValueError("An experiment cannot supersede itself")
        object.__setattr__(
            self,
            "supersedes_experiment_ids",
            tuple(sorted(supersedes_experiment_ids, key=str)),
        )
        object.__setattr__(self, "input_refs", tuple(self.input_refs))
        object.__setattr__(self, "preconditions", tuple(self.preconditions))
        object.__setattr__(self, "step_ids", tuple(dict.fromkeys(self.step_ids)))
        object.__setattr__(self, "tool_invocation_refs", tuple(self.tool_invocation_refs))
        object.__setattr__(self, "observation_refs", tuple(self.observation_refs))
        object.__setattr__(self, "limitations", tuple(self.limitations))
        object.__setattr__(self, "next_pivots", tuple(self.next_pivots))
        if not self.claim_ids or not all(
            isinstance(item, str) and item.strip() for item in self.claim_ids
        ):
            raise ValueError("An experiment must reference at least one article claim")
        if not self.input_refs:
            raise ValueError("An experiment must identify at least one input by type and ID")
        if (
            len(self.step_ids) > 64
            or len(self.tool_invocation_refs) > 64
            or len(self.observation_refs) > 128
        ):
            raise ValueError("Experiment evidence references exceed their limits")
        if len(self.tool_arguments) > 128:
            raise ValueError("Tool arguments exceed the supported key count")
        if len(self.next_pivots) > 128 or any(
            not isinstance(item, str) or not _SHA256.fullmatch(item) for item in self.next_pivots
        ):
            raise ValueError("Next pivots must reference stable C1 frontier node IDs")
        _validate_safe_arguments(self.tool_arguments)
        object.__setattr__(self, "tool_arguments", freeze_json(self.tool_arguments))
        for name, value in (
            ("started_at", self.started_at),
            ("completed_at", self.completed_at),
            ("recorded_at", self.recorded_at),
        ):
            _aware(value, name)
        if self.completed_at < self.started_at or self.recorded_at < self.completed_at:
            raise ValueError("Experiment dates must be chronological")
        if (
            self.actual_outcome is ObservationOutcome.NOT_OBSERVED
            and self.observation_coverage is not ObservationCoverage.COMPLETE
            and self.verdict is ExperimentVerdict.REFUTES
        ):
            raise ValueError(
                "Limited or absent visibility cannot refute a claim by non-observation"
            )
        if (
            self.verdict
            in {
                ExperimentVerdict.SUPPORTS,
                ExperimentVerdict.REFUTES,
                ExperimentVerdict.QUALIFIES,
            }
            and not self.observation_refs
        ):
            raise ValueError("A conclusive experiment verdict must cite persisted observations")
        if self.observation_coverage is not ObservationCoverage.COMPLETE and not self.limitations:
            raise ValueError("Limited observation coverage must state its limitations")

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "id": str(self.id),
            "investigation_id": str(self.investigation_id),
            "version": self.version,
            "hypothesis_id": self.hypothesis_id,
            "claim_ids": list(self.claim_ids),
            "working_state_version": self.working_state_version,
            "g2_envelope_version": self.g2_envelope_version,
            "prediction": self.prediction,
            "falsification_criterion": self.falsification_criterion,
            "method": self.method,
            "method_mode": self.method_mode.value,
            "input_type": self.input_type,
            "input_refs": [item.to_payload() for item in self.input_refs],
            "preconditions": list(self.preconditions),
            "availability": self.availability.value,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "tool_schema_version": self.tool_schema_version,
            "tool_arguments": thaw_json(self.tool_arguments),
            "step_ids": list(self.step_ids),
            "tool_invocation_refs": [item.to_payload() for item in self.tool_invocation_refs],
            "observation_refs": [item.to_payload() for item in self.observation_refs],
            "expected": self.expected,
            "actual": self.actual,
            "actual_outcome": self.actual_outcome.value,
            "observation_coverage": self.observation_coverage.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "scope": self.scope,
            "limitations": list(self.limitations),
            "verdict": self.verdict.value,
            "implication": self.implication,
            "next_pivots": list(self.next_pivots),
            "author": self.author,
            "origin": self.origin,
        }
        # Omit the additive field when empty so legacy payload identity hashes stay stable.
        if self.supersedes_experiment_ids:
            payload["supersedes_experiment_ids"] = [
                str(item) for item in self.supersedes_experiment_ids
            ]
        return payload

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_payload())).hexdigest()

    @property
    def identity_sha256(self) -> str:
        payload = self.to_payload()
        payload.pop("version")
        payload.pop("recorded_at")
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> InvestigationExperimentV1:
        try:
            return cls(
                id=UUID(str(payload["id"])),
                investigation_id=UUID(str(payload["investigation_id"])),
                version=int(payload["version"]),
                schema_version=str(payload["schema_version"]),
                supersedes_experiment_ids=tuple(
                    UUID(str(item)) for item in payload.get("supersedes_experiment_ids", ())
                ),
                hypothesis_id=str(payload["hypothesis_id"]),
                claim_ids=tuple(str(item) for item in payload["claim_ids"]),
                working_state_version=int(payload["working_state_version"]),
                g2_envelope_version=int(payload["g2_envelope_version"]),
                prediction=str(payload["prediction"]),
                falsification_criterion=str(payload["falsification_criterion"]),
                method=str(payload["method"]),
                method_mode=ExperimentMethodMode(str(payload.get("method_mode", "passive"))),
                input_type=str(payload["input_type"]),
                input_refs=tuple(ExperimentInputRefV1(**item) for item in payload["input_refs"]),
                preconditions=tuple(str(item) for item in payload["preconditions"]),
                availability=ExperimentAvailability(str(payload["availability"])),
                tool_name=str(payload["tool_name"]),
                tool_version=str(payload["tool_version"]),
                tool_schema_version=str(payload["tool_schema_version"]),
                tool_arguments=payload["tool_arguments"],
                step_ids=tuple(str(item) for item in payload["step_ids"]),
                tool_invocation_refs=tuple(
                    ExperimentInvocationRefV1(
                        invocation_id=UUID(str(item["invocation_id"])),
                        version=int(item["version"]),
                    )
                    for item in payload["tool_invocation_refs"]
                ),
                observation_refs=tuple(
                    ExperimentObservationRefV1(
                        observation_id=UUID(str(item["observation_id"])), sha256=str(item["sha256"])
                    )
                    for item in payload["observation_refs"]
                ),
                expected=str(payload["expected"]),
                actual=str(payload["actual"]),
                actual_outcome=ObservationOutcome(str(payload["actual_outcome"])),
                observation_coverage=ObservationCoverage(str(payload["observation_coverage"])),
                started_at=datetime.fromisoformat(str(payload["started_at"])),
                completed_at=datetime.fromisoformat(str(payload["completed_at"])),
                recorded_at=datetime.fromisoformat(str(payload["recorded_at"])),
                scope=str(payload["scope"]),
                limitations=tuple(str(item) for item in payload["limitations"]),
                verdict=ExperimentVerdict(str(payload["verdict"])),
                implication=str(payload["implication"]),
                next_pivots=tuple(str(item) for item in payload["next_pivots"]),
                author=str(payload["author"]),
                origin=str(payload["origin"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid investigation experiment payload") from exc


@dataclass(frozen=True, slots=True)
class ClaimExperimentScopeV1:
    experiment_id: UUID
    scope: str
    verdict: ExperimentVerdict
    observation_ids: tuple[UUID, ...]
    superseded_by: tuple[UUID, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "experiment_id": str(self.experiment_id),
            "scope": self.scope,
            "verdict": self.verdict.value,
            "observation_ids": [str(item) for item in self.observation_ids],
            "superseded_by": [str(item) for item in self.superseded_by],
        }


@dataclass(frozen=True, slots=True)
class ConsolidatedClaimVerdictV1:
    claim_id: str
    status: ConsolidatedClaimStatus
    verdicts: tuple[ExperimentVerdict, ...]
    experiment_ids: tuple[UUID, ...]
    scopes: tuple[ClaimExperimentScopeV1, ...]
    contradictory: bool
    superseded_by: tuple[UUID, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "status": self.status.value,
            "verdicts": [item.value for item in self.verdicts],
            "experiment_ids": [str(item) for item in self.experiment_ids],
            "scopes": [item.to_payload() for item in self.scopes],
            "contradictory": self.contradictory,
            "superseded_by": [str(item) for item in self.superseded_by],
        }


def consolidate_claim_experiments(
    experiments: tuple[InvestigationExperimentV1, ...],
) -> tuple[ConsolidatedClaimVerdictV1, ...]:
    by_claim: dict[str, list[InvestigationExperimentV1]] = {}
    ordered_experiments = tuple(sorted(experiments, key=lambda item: (item.version, str(item.id))))
    superseded_ids = {
        superseded_id
        for experiment in ordered_experiments
        for superseded_id in experiment.supersedes_experiment_ids
    }
    superseded_by: dict[UUID, list[UUID]] = {}
    for experiment in ordered_experiments:
        for superseded_id in experiment.supersedes_experiment_ids:
            superseded_by.setdefault(superseded_id, []).append(experiment.id)
    for experiment in ordered_experiments:
        for claim_id in experiment.claim_ids:
            by_claim.setdefault(claim_id, []).append(experiment)
    result: list[ConsolidatedClaimVerdictV1] = []
    for claim_id, items in sorted(by_claim.items()):
        active_items = tuple(item for item in items if item.id not in superseded_ids)
        verdicts = tuple(dict.fromkeys(item.verdict for item in active_items))
        decisive = set(verdicts) & {ExperimentVerdict.SUPPORTS, ExperimentVerdict.REFUTES}
        contradictory = len(decisive) == 2
        latest_verdict = active_items[-1].verdict if active_items else None
        if contradictory:
            status = ConsolidatedClaimStatus.QUALIFIED
        elif latest_verdict is ExperimentVerdict.SUPPORTS:
            status = ConsolidatedClaimStatus.SUPPORTED
        elif latest_verdict is ExperimentVerdict.REFUTES:
            status = ConsolidatedClaimStatus.REFUTED
        elif latest_verdict is ExperimentVerdict.QUALIFIES:
            status = ConsolidatedClaimStatus.QUALIFIED
        else:
            status = ConsolidatedClaimStatus.INCONCLUSIVE
        result.append(
            ConsolidatedClaimVerdictV1(
                claim_id=claim_id,
                status=status,
                verdicts=verdicts,
                experiment_ids=tuple(item.id for item in items),
                scopes=tuple(
                    ClaimExperimentScopeV1(
                        experiment_id=item.id,
                        scope=item.scope,
                        verdict=item.verdict,
                        observation_ids=tuple(ref.observation_id for ref in item.observation_refs),
                        superseded_by=tuple(superseded_by.get(item.id, ())),
                    )
                    for item in items
                ),
                contradictory=contradictory,
                superseded_by=tuple(
                    dict.fromkeys(
                        superseding_id
                        for item in items
                        for superseding_id in superseded_by.get(item.id, ())
                    )
                ),
            )
        )
    return tuple(result)


__all__ = [
    "EXPERIMENT_SCHEMA_VERSION",
    "EXPERIMENT_TIMELINE_SCHEMA_VERSION",
    "EXPERIMENT_TOOL_SCHEMA_VERSION",
    "HYPOTHESIS_DECLARE_TOOL_SCHEMA_VERSION",
    "HYPOTHESIS_ID_PATTERN",
    "ClaimExperimentScopeV1",
    "ConsolidatedClaimStatus",
    "ConsolidatedClaimVerdictV1",
    "ExperimentAvailability",
    "ExperimentInputRefV1",
    "ExperimentInvocationRefV1",
    "ExperimentMethodMode",
    "ExperimentObservationRefV1",
    "ExperimentVerdict",
    "InvestigationExperimentV1",
    "ObservationCoverage",
    "ObservationOutcome",
    "consolidate_claim_experiments",
]

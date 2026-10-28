"""Pure domain contracts for an evidence-led investigation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4


class ToolInvocationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    POLICY_DENIED = "policy_denied"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    PARTIAL = "partial"


class FrameworkPreSubmissionProof(StrEnum):
    """Framework-attested outcomes proving no external submit or handoff occurred."""

    INVALID_ARGUMENTS = "framework_pre_submission_invalid_arguments"
    TOOL_UNAVAILABLE = "framework_pre_submission_tool_unavailable"
    POLICY_REJECTED = "framework_pre_submission_policy_rejected"
    BUDGET_REJECTED = "framework_pre_submission_budget_rejected"
    ENVELOPE_ADMISSION_CONFLICT = "framework_pre_submission_envelope_admission_conflict"
    ENVELOPE_REVOKED = "framework_pre_submission_envelope_revoked"
    RESERVATION_CHANGED = "framework_pre_submission_reservation_changed"
    INVESTIGATION_MISSING = "framework_pre_submission_investigation_missing"
    DISCLOSURE_REVOKED = "framework_pre_submission_disclosure_revoked"
    OPERATOR_INTERRUPTED = "framework_pre_submission_operator_interrupted"
    EXTERNAL_EFFECT_PRE_SEND_REFUSAL = "framework_pre_submission_external_effect_refused"


class InvestigationExecutionMode(StrEnum):
    BRIDGE_LED = "bridge_led"
    OPERATOR_LED = "operator_led"


class EvidenceKind(StrEnum):
    VT_FILE_REPORT = "vt_file_report"
    VT_RELATIONSHIP = "vt_relationship"
    VT_BEHAVIOUR = "vt_behaviour"
    SHODAN_OBSERVATION = "shodan_observation"
    BINARY_ANALYSIS = "binary_analysis"
    BINARY_STRING = "binary_string"
    YARA_MATCH = "yara_match"
    NEGATIVE_RESULT = "negative_result"
    CLAIM_SOURCE = "claim_source"
    OTHER = "other"


class FindingKind(StrEnum):
    NEW_SAMPLE = "new_sample"
    INFECTION_CHAIN = "infection_chain"
    CAPABILITY = "capability"
    NETWORK_BEHAVIOUR = "network_behaviour"
    CLAIM_VERIFICATION = "claim_verification"
    DETECTION = "detection"


class FindingAssessment(StrEnum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    QUALIFIED = "qualified"
    INCONCLUSIVE = "inconclusive"


class NoveltyClass(StrEnum):
    NEW_TO_ARTICLE = "new_to_article"
    ALREADY_DOCUMENTED_BUT_NEW_DETAIL = "already_documented_but_new_detail"
    CLAIM_CORRECTION = "claim_correction"
    NOT_CONFIRMED = "not_confirmed"


class SampleOrigin(StrEnum):
    SEED = "seed"
    VT_PIVOT = "vt_pivot"
    RELATIONSHIP = "relationship"
    BEHAVIOR = "behavior"
    MANUAL = "manual"


class SampleQualification(StrEnum):
    CANDIDATE = "candidate"
    RELATED = "related"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class QualificationActorKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class DetectionCandidateStatus(StrEnum):
    CANDIDATE = "candidate"
    TESTED = "tested"
    REJECTED = "rejected"
    APPROVED = "approved"
    EXPERIMENTAL = "experimental"


class BudgetDimension(StrEnum):
    BRIDGE_TURNS = "bridge_turns"
    VT_UNITS = "vt_units"
    SHODAN_CREDITS = "shodan_credits"
    DOWNLOADS = "downloads"
    TOOL_SECONDS = "tool_seconds"
    CONTEXT_TOKENS = "context_tokens"
    CONTEXT_BYTES = "context_bytes"


class BudgetReservationStatus(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"


class AgentNextAction(StrEnum):
    CONTINUE = "continue"
    SWITCH_HYPOTHESIS = "switch_hypothesis"
    DRAFT_OPINION = "draft_opinion"
    FINISH = "finish"
    REQUEST_PERMISSION = "request_permission"


class TLPLevel(StrEnum):
    WHITE = "white"
    CLEAR = "clear"
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class BudgetExceeded(Exception):
    """Raised when a multidimensional budget cannot reserve one dimension."""

    def __init__(self, dimension: BudgetDimension) -> None:
        self.dimension = dimension
        super().__init__(f"Investigation budget exceeded for {dimension.value}")


def _aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _sha256(value: str, field_name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field_name} must be lowercase SHA-256")


def _require_enum(value: object, enum_type: type[StrEnum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field_name} must use {enum_type.__name__}")


def freeze_json(value: Any) -> Any:
    """Copy JSON-shaped data into immutable containers and reject non-JSON values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(item) for item in value)
    raise ValueError(f"Value of type {type(value).__name__} is not JSON data")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical UTF-8 JSON representation shared by pure domain contracts."""
    return json.dumps(
        thaw_json(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _budget_values(values: Mapping[BudgetDimension, int]) -> Mapping[BudgetDimension, int]:
    clean: dict[BudgetDimension, int] = {}
    for dimension, amount in values.items():
        if not isinstance(dimension, BudgetDimension):
            raise TypeError("Budget dimensions must use BudgetDimension")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("Budget amounts must be non-negative integers")
        clean[dimension] = amount
    return MappingProxyType(clean)


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    operation_key: str
    amounts: Mapping[BudgetDimension, int]
    status: BudgetReservationStatus = BudgetReservationStatus.RESERVED
    committed_amounts: Mapping[BudgetDimension, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_enum(self.status, BudgetReservationStatus, "status")
        if not self.operation_key.strip():
            raise ValueError("operation_key must not be empty")
        object.__setattr__(self, "amounts", _budget_values(self.amounts))
        object.__setattr__(self, "committed_amounts", _budget_values(self.committed_amounts))
        if any(
            dimension is not BudgetDimension.TOOL_SECONDS
            and amount > self.amounts.get(dimension, 0)
            for dimension, amount in self.committed_amounts.items()
        ):
            raise ValueError("Committed budget cannot exceed its reservation")


@dataclass(slots=True)
class InvestigationBudget:
    """Configurable caps with idempotent, multidimensional reserve/commit/release."""

    limits: Mapping[BudgetDimension, int] = field(default_factory=dict)
    _reservations: dict[str, BudgetReservation] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.limits = _budget_values(self.limits)

    @property
    def reservations(self) -> tuple[BudgetReservation, ...]:
        return tuple(self._reservations.values())

    @property
    def consumed(self) -> Mapping[BudgetDimension, int]:
        totals = {dimension: 0 for dimension in BudgetDimension}
        for reservation in self._reservations.values():
            for dimension, amount in reservation.committed_amounts.items():
                totals[dimension] += amount
        return MappingProxyType(totals)

    @property
    def reserved(self) -> Mapping[BudgetDimension, int]:
        totals = {dimension: 0 for dimension in BudgetDimension}
        for reservation in self._reservations.values():
            if reservation.status is BudgetReservationStatus.RESERVED:
                for dimension, amount in reservation.amounts.items():
                    totals[dimension] += amount
        return MappingProxyType(totals)

    @property
    def remaining(self) -> Mapping[BudgetDimension, int | None]:
        consumed, reserved = self.consumed, self.reserved
        return MappingProxyType(
            {
                dimension: (
                    max(0, limit - consumed[dimension] - reserved[dimension])
                    if (limit := self.limits.get(dimension)) is not None
                    else None
                )
                for dimension in BudgetDimension
            }
        )

    def reserve(
        self, operation_key: str, amounts: Mapping[BudgetDimension, int]
    ) -> BudgetReservation:
        requested = _budget_values(amounts)
        existing = self._reservations.get(operation_key)
        if existing is not None:
            if dict(existing.amounts) != dict(requested):
                raise ValueError("An operation key cannot be reused with a different reservation")
            return existing
        for dimension, amount in requested.items():
            # Context bytes and tool time are recorded for accounting, not used as caps.
            if dimension in (BudgetDimension.CONTEXT_BYTES, BudgetDimension.TOOL_SECONDS):
                continue
            limit = self.limits.get(dimension)
            if limit is not None and (
                self.consumed[dimension] + self.reserved[dimension] + amount > limit
            ):
                raise BudgetExceeded(dimension)
        reservation = BudgetReservation(operation_key=operation_key, amounts=requested)
        self._reservations[operation_key] = reservation
        return reservation

    def restore_reservation(self, reservation: BudgetReservation) -> BudgetReservation:
        """Restore a canonical reservation record after a policy snapshot changes."""
        if not isinstance(reservation, BudgetReservation):
            raise TypeError("reservation must be a BudgetReservation")
        existing = self._reservations.get(reservation.operation_key)
        if existing is None:
            self._reservations[reservation.operation_key] = reservation
            return reservation
        if existing == reservation:
            return existing
        if (
            existing.status is not BudgetReservationStatus.RESERVED
            or reservation.status is not BudgetReservationStatus.RESERVED
        ):
            raise ValueError("Only a reserved operation can be restored")
        if any(
            reservation.amounts.get(dimension) != amount
            for dimension, amount in existing.amounts.items()
        ):
            raise ValueError("A restored reservation cannot change existing amounts")
        self._reservations[reservation.operation_key] = reservation
        return reservation

    def commit(
        self,
        operation_key: str,
        consumed: Mapping[BudgetDimension, int] | None = None,
    ) -> BudgetReservation:
        existing = self._reservations.get(operation_key)
        if existing is None:
            raise KeyError(operation_key)
        if existing.status is BudgetReservationStatus.COMMITTED:
            return existing
        if existing.status is BudgetReservationStatus.RELEASED:
            raise ValueError("A released reservation cannot be committed")
        actual = _budget_values(consumed if consumed is not None else existing.amounts)
        if any(
            dimension is not BudgetDimension.TOOL_SECONDS
            and amount > existing.amounts.get(dimension, 0)
            for dimension, amount in actual.items()
        ):
            raise ValueError("Consumed budget cannot exceed the reservation")
        committed = replace(
            existing,
            status=BudgetReservationStatus.COMMITTED,
            committed_amounts=actual,
        )
        self._reservations[operation_key] = committed
        return committed

    def release(self, operation_key: str) -> BudgetReservation | None:
        existing = self._reservations.get(operation_key)
        if existing is None or existing.status is not BudgetReservationStatus.RESERVED:
            return existing
        released = replace(existing, status=BudgetReservationStatus.RELEASED)
        self._reservations[operation_key] = released
        return released

    def release_confirmed_non_submission(self, operation_key: str) -> BudgetReservation | None:
        """Release a Bridge reservation when durable evidence proves no request was sent.

        This also reverses a committed reservation left by older workers that charged
        a Bridge turn before persisting the provider's explicit pre-submission result.
        """
        existing = self._reservations.get(operation_key)
        if existing is None or existing.status is BudgetReservationStatus.RELEASED:
            return existing
        released = replace(
            existing,
            status=BudgetReservationStatus.RELEASED,
            committed_amounts={},
        )
        self._reservations[operation_key] = released
        return released


_SECRET_KEY = re.compile(
    r"(?:^|[_-])(api[_-]?key|token|secret|password|authorization|credential|cookie|signature)(?:$|[_-])",
    re.IGNORECASE,
)
_SIGNED_QUERY = re.compile(
    r"(?:[?&](?:x-amz-[^=]+|signature|sig|token|access_token|expires)=)", re.IGNORECASE
)


def _validate_safe_parameters(value: Any, path: str = "parameters") -> None:
    if isinstance(value, bytes | bytearray | memoryview):
        raise ValueError(f"{path} cannot contain binary data")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Normalized parameter keys must be strings")
            if _SECRET_KEY.search(key.replace(" ", "_")):
                raise ValueError(f"{path}.{key} may contain a secret")
            _validate_safe_parameters(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_safe_parameters(item, f"{path}[{index}]")
    elif isinstance(value, str) and _SIGNED_QUERY.search(value):
        raise ValueError(f"{path} cannot contain a signed URL")


def tool_operation_key(
    *,
    investigation_id: UUID,
    tool_name: str,
    schema_version: str,
    normalized_parameters: Mapping[str, Any],
    purpose: str,
    cache_identity: Mapping[str, Any] | None = None,
) -> str:
    """Build a stable operation identity from the operation and normalized input.

    ``purpose`` remains in the API for existing callers and invocation records,
    but explanatory model text must not split an otherwise identical operation.
    Confined local analyses may provide a result/worker cache identity; callers
    that omit it retain their historical key shape, including external tools.
    """
    _validate_safe_parameters(normalized_parameters)
    if cache_identity is not None:
        _validate_safe_parameters(cache_identity, "cache_identity")
    payload = {
        "investigation_id": str(investigation_id),
        "tool_name": tool_name,
        "schema_version": schema_version,
        "parameters": normalized_parameters,
    }
    if cache_identity is not None:
        payload["cache_identity"] = cache_identity
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolInvocation:
    investigation_id: UUID
    operation_key: str
    tool_name: str
    schema_version: str
    normalized_parameters: Mapping[str, Any]
    purpose: str
    status: ToolInvocationStatus = ToolInvocationStatus.PENDING
    reserved_budget: Mapping[BudgetDimension, int] = field(default_factory=dict)
    consumed_budget: Mapping[BudgetDimension, int] = field(default_factory=dict)
    response_refs: tuple[UUID, ...] = ()
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: int = 1

    def __post_init__(self) -> None:
        _require_enum(self.status, ToolInvocationStatus, "status")
        if (
            not self.operation_key.strip()
            or not self.tool_name.strip()
            or not self.schema_version.strip()
        ):
            raise ValueError("Tool operation identity fields must not be empty")
        if not self.purpose.strip():
            raise ValueError("Tool invocation purpose must not be empty")
        if not isinstance(self.normalized_parameters, Mapping):
            raise TypeError("normalized_parameters must be a JSON object")
        _validate_safe_parameters(self.normalized_parameters)
        object.__setattr__(self, "normalized_parameters", freeze_json(self.normalized_parameters))
        object.__setattr__(self, "reserved_budget", _budget_values(self.reserved_budget))
        object.__setattr__(self, "consumed_budget", _budget_values(self.consumed_budget))
        if self.version < 1:
            raise ValueError("version must be >= 1")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")

    def transition(
        self,
        status: ToolInvocationStatus,
        *,
        response_refs: tuple[UUID, ...] | None = None,
        consumed_budget: Mapping[BudgetDimension, int] | None = None,
        now: datetime | None = None,
    ) -> ToolInvocation:
        _require_enum(status, ToolInvocationStatus, "status")
        terminal = {
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.EMPTY,
            ToolInvocationStatus.UNAVAILABLE,
            ToolInvocationStatus.POLICY_DENIED,
            ToolInvocationStatus.NEEDS_REVIEW,
            ToolInvocationStatus.FAILED,
            ToolInvocationStatus.PARTIAL,
        }
        allowed = (
            self.status is status
            or (
                self.status is ToolInvocationStatus.PENDING
                and status in terminal | {ToolInvocationStatus.RUNNING}
            )
            or (self.status is ToolInvocationStatus.RUNNING and status in terminal)
        )
        if not allowed:
            raise ValueError(
                f"Invalid tool invocation transition: {self.status.value} -> {status.value}"
            )
        if status is self.status and response_refs is None and consumed_budget is None:
            return self
        return replace(
            self,
            status=status,
            response_refs=self.response_refs if response_refs is None else response_refs,
            consumed_budget=(
                self.consumed_budget if consumed_budget is None else _budget_values(consumed_budget)
            ),
            updated_at=now or datetime.now(UTC),
            version=self.version + 1,
        )

    def resolve_pre_submission_failure(
        self,
        *,
        now: datetime | None = None,
    ) -> ToolInvocation:
        """Resolve a reviewed invocation only after its caller proves no submit occurred."""
        if self.status is not ToolInvocationStatus.NEEDS_REVIEW:
            raise ValueError("Only a needs-review invocation can be resolved as unsubmitted")
        return replace(
            self,
            status=ToolInvocationStatus.FAILED,
            consumed_budget={},
            updated_at=now or datetime.now(UTC),
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceObservation:
    investigation_id: UUID
    kind: EvidenceKind
    blob_id: UUID
    sha256: str
    source: str
    source_version: str
    observed_at: datetime
    object_type: str
    object_id: str
    limitations: tuple[str, ...] = ()
    invocation_ref: UUID | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        _require_enum(self.kind, EvidenceKind, "kind")
        _sha256(self.sha256, "sha256")
        if not all(
            value.strip()
            for value in (self.source, self.source_version, self.object_type, self.object_id)
        ):
            raise ValueError("Observation provenance and object identity must not be empty")
        _aware(self.observed_at, "observed_at")
        object.__setattr__(self, "limitations", tuple(self.limitations))


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationFinding:
    investigation_id: UUID
    kind: FindingKind
    assessment: FindingAssessment
    statement: str
    observation_refs: tuple[UUID, ...]
    claim_refs: tuple[str, ...] = ()
    novelty: NoveltyClass | None = None
    scope: str = ""
    limitations: tuple[str, ...] = ()
    id: UUID = field(default_factory=uuid4)
    version: int = 1
    supersedes: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_enum(self.kind, FindingKind, "kind")
        _require_enum(self.assessment, FindingAssessment, "assessment")
        if self.novelty is not None:
            _require_enum(self.novelty, NoveltyClass, "novelty")
        if not self.statement.strip():
            raise ValueError("Finding statement must not be empty")
        if self.assessment is FindingAssessment.SUPPORTED and not self.observation_refs:
            raise ValueError("A supported finding must cite at least one observation")
        if self.version < 1 or ((self.version == 1) != (self.supersedes is None)):
            raise ValueError("Finding versions must link to the preceding version")
        if self.supersedes == self.id:
            raise ValueError("A finding version cannot supersede itself")
        _aware(self.created_at, "created_at")
        object.__setattr__(self, "observation_refs", tuple(self.observation_refs))
        object.__setattr__(self, "claim_refs", tuple(self.claim_refs))
        object.__setattr__(self, "limitations", tuple(self.limitations))


@dataclass(frozen=True, slots=True, kw_only=True)
class QualificationChange:
    from_qualification: SampleQualification
    to_qualification: SampleQualification
    actor_kind: QualificationActorKind
    actor_id: str | None
    reason: str
    changed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_enum(self.from_qualification, SampleQualification, "from_qualification")
        _require_enum(self.to_qualification, SampleQualification, "to_qualification")
        _require_enum(self.actor_kind, QualificationActorKind, "actor_kind")
        if not self.reason.strip():
            raise ValueError("Qualification change requires a reason")
        if self.to_qualification is SampleQualification.CONFIRMED and (
            self.actor_kind is not QualificationActorKind.HUMAN or not (self.actor_id or "").strip()
        ):
            raise ValueError("Only an identified human actor can confirm a sample association")
        if not (self.actor_id or "").strip():
            raise ValueError("Qualification changes require an actor identity")
        _aware(self.changed_at, "changed_at")


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleAssociation:
    investigation_id: UUID
    sample_sha256: str
    origin: SampleOrigin
    qualification: SampleQualification = SampleQualification.CANDIDATE
    changes: tuple[QualificationChange, ...] = ()
    related_sample_sha256: tuple[str, ...] = ()
    id: UUID = field(default_factory=uuid4)
    version: int = 1

    def __post_init__(self) -> None:
        _require_enum(self.origin, SampleOrigin, "origin")
        _require_enum(self.qualification, SampleQualification, "qualification")
        _sha256(self.sample_sha256, "sample_sha256")
        if not all(isinstance(change, QualificationChange) for change in self.changes):
            raise TypeError("Sample association history must contain QualificationChange")
        for related_sha256 in self.related_sample_sha256:
            _sha256(related_sha256, "related_sample_sha256")
        if self.version < 1 or self.version != len(self.changes) + 1:
            raise ValueError("Sample association version must match its append-only change history")
        if self.changes and self.changes[-1].to_qualification is not self.qualification:
            raise ValueError("Current qualification must match the last qualification change")
        for index in range(1, len(self.changes)):
            if (
                self.changes[index - 1].to_qualification
                is not self.changes[index].from_qualification
            ):
                raise ValueError("Qualification changes must form one continuous history")
        if self.qualification is SampleQualification.CONFIRMED and not self.changes:
            raise ValueError("Confirmed qualification requires an identified human change")
        object.__setattr__(self, "changes", tuple(self.changes))
        object.__setattr__(self, "related_sample_sha256", tuple(self.related_sample_sha256))

    def change_qualification(
        self,
        qualification: SampleQualification,
        *,
        actor_kind: QualificationActorKind,
        actor_id: str | None,
        reason: str,
        changed_at: datetime | None = None,
    ) -> SampleAssociation:
        if qualification is self.qualification:
            return self
        change = QualificationChange(
            from_qualification=self.qualification,
            to_qualification=qualification,
            actor_kind=actor_kind,
            actor_id=actor_id,
            reason=reason,
            changed_at=changed_at or datetime.now(UTC),
        )
        return replace(
            self,
            qualification=qualification,
            changes=(*self.changes, change),
            version=self.version + 1,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MotifProvenance:
    motif: str
    observation_refs: tuple[UUID, ...]
    rationale: str

    def __post_init__(self) -> None:
        if not self.motif.strip() or not self.rationale.strip() or not self.observation_refs:
            raise ValueError("Motif provenance requires a motif, rationale, and observation")
        object.__setattr__(self, "observation_refs", tuple(self.observation_refs))


@dataclass(frozen=True, slots=True, kw_only=True)
class DetectionCandidateVersion:
    investigation_id: UUID
    source: str
    source_sha256: str
    motif_provenance: tuple[MotifProvenance, ...]
    status: DetectionCandidateStatus = DetectionCandidateStatus.CANDIDATE
    candidate_id: UUID = field(default_factory=uuid4)
    version: int = 1
    parent_version_id: UUID | None = None
    test_refs: tuple[UUID, ...] = ()
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require_enum(self.status, DetectionCandidateStatus, "status")
        _sha256(self.source_sha256, "source_sha256")
        if hashlib.sha256(self.source.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("YARA source SHA-256 does not match source")
        if self.version < 1 or ((self.version == 1) != (self.parent_version_id is None)):
            raise ValueError("Detection versions must link to a parent version after v1")
        if not self.source.strip():
            raise ValueError("Detection source must not be empty")
        _aware(self.created_at, "created_at")
        object.__setattr__(self, "motif_provenance", tuple(self.motif_provenance))
        object.__setattr__(self, "test_refs", tuple(self.test_refs))

    @property
    def version_label(self) -> str:
        return f"v{self.version}"


@dataclass(frozen=True, slots=True)
class DisclosureBounds:
    tlp: TLPLevel | None = None
    do_not_submit: bool = False
    external_llm_allowed: bool = False

    def __post_init__(self) -> None:
        if self.tlp is not None:
            _require_enum(self.tlp, TLPLevel, "tlp")
        if not isinstance(self.do_not_submit, bool) or not isinstance(
            self.external_llm_allowed, bool
        ):
            raise TypeError("Disclosure policy flags must be booleans")


class EnvelopeAuthorityKind(StrEnum):
    HUMAN_G2 = "human_g2"
    OSINT_POLICY = "osint_policy"


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationEnvelope:
    investigation_id: UUID
    allowed_providers: tuple[str, ...]
    budget: InvestigationBudget
    acquire_hits_allowed: bool
    disclosure: DisclosureBounds
    approval_decision_id: UUID
    approval_actor_id: str
    authority_kind: EnvelopeAuthorityKind = EnvelopeAuthorityKind.HUMAN_G2
    inherited_policy_id: UUID | None = None
    inherited_policy_version: int | None = None
    inherited_policy_event_id: UUID | None = None
    approved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.budget, InvestigationBudget):
            raise TypeError("Investigation envelope requires an InvestigationBudget")
        if not isinstance(self.disclosure, DisclosureBounds):
            raise TypeError("Investigation envelope requires DisclosureBounds")
        if not isinstance(self.acquire_hits_allowed, bool):
            raise TypeError("acquire_hits_allowed must be a boolean")
        if not self.approval_actor_id.strip():
            raise ValueError("An approved envelope requires an identified actor")
        if not isinstance(self.authority_kind, EnvelopeAuthorityKind):
            raise TypeError("Envelope authority must use EnvelopeAuthorityKind")
        inherited_fields = (
            self.inherited_policy_id,
            self.inherited_policy_version,
            self.inherited_policy_event_id,
        )
        if self.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY:
            if any(item is None for item in inherited_fields):
                raise ValueError("An inherited envelope must identify its policy version and event")
            if self.inherited_policy_version is None or self.inherited_policy_version < 1:
                raise ValueError("Inherited policy version must be positive")
            if self.approval_decision_id != self.inherited_policy_event_id:
                raise ValueError("Inherited envelope authority must reference its policy event")
        elif any(item is not None for item in inherited_fields):
            raise ValueError("A HUMAN_G2 envelope cannot retain inherited policy references")
        if any(not provider.strip() for provider in self.allowed_providers):
            raise ValueError("Allowed provider names must not be empty")
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if len(set(self.allowed_providers)) != len(self.allowed_providers):
            raise ValueError("Allowed providers must be unique")
        _aware(self.approved_at, "approved_at")
        object.__setattr__(self, "allowed_providers", tuple(self.allowed_providers))


@dataclass(frozen=True, slots=True)
class AgendaItem:
    question: str
    potential: float
    rank: int

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("Agenda question must not be empty")
        if isinstance(self.potential, bool) or not 0.0 <= self.potential <= 1.0:
            raise ValueError("Agenda potential must be between 0 and 1")
        if isinstance(self.rank, bool) or self.rank < 1:
            raise ValueError("Agenda rank must be >= 1")


@dataclass(frozen=True, slots=True)
class InvestigationAgenda:
    items: tuple[AgendaItem, ...]

    def __post_init__(self) -> None:
        if not all(isinstance(item, AgendaItem) for item in self.items):
            raise TypeError("Investigation agenda items must use AgendaItem")
        if not 2 <= len(self.items) <= 6:
            raise ValueError("An investigation agenda must contain 2 to 6 questions")
        if tuple(item.rank for item in self.items) != tuple(range(1, len(self.items) + 1)):
            raise ValueError("Agenda items must be ordered by consecutive rank")
        potentials = tuple(item.potential for item in self.items)
        if potentials != tuple(sorted(potentials, reverse=True)):
            raise ValueError("Agenda items must be ranked by descending potential")
        if len({item.question for item in self.items}) != len(self.items):
            raise ValueError("Agenda questions must be unique")


def investigation_budget_from_loop_budget(loop_budget: Any) -> InvestigationBudget:
    """Project the legacy cycle cap into Bridge turns without mutating LoopBudget."""
    cycles = getattr(loop_budget, "max_cycles", None)
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
        raise ValueError("LoopBudget must expose a non-negative max_cycles")
    return InvestigationBudget(limits={BudgetDimension.BRIDGE_TURNS: cycles})


__all__ = [
    "AgendaItem",
    "AgentNextAction",
    "BudgetDimension",
    "BudgetExceeded",
    "BudgetReservation",
    "BudgetReservationStatus",
    "DetectionCandidateStatus",
    "DetectionCandidateVersion",
    "DisclosureBounds",
    "EnvelopeAuthorityKind",
    "EvidenceKind",
    "EvidenceObservation",
    "FindingAssessment",
    "FindingKind",
    "InvestigationAgenda",
    "InvestigationBudget",
    "InvestigationEnvelope",
    "InvestigationFinding",
    "MotifProvenance",
    "NoveltyClass",
    "QualificationActorKind",
    "QualificationChange",
    "SampleAssociation",
    "SampleOrigin",
    "SampleQualification",
    "TLPLevel",
    "ToolInvocation",
    "ToolInvocationStatus",
    "canonical_json_bytes",
    "freeze_json",
    "investigation_budget_from_loop_budget",
    "thaw_json",
    "tool_operation_key",
]

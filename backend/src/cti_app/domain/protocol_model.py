"""Canonical, additive semantic contracts for passive protocol analysis."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.network_protocol import (
    NetworkExtractionLimitsV1,
    NetworkTransport,
    StreamDirection,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHORT_HEX = re.compile(r"^(?:[0-9a-f]{2}){1,16}$")


class ProtocolValidationStatus(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"
    CONTRADICTED = "contradicted"
    DOCUMENTED_NOT_OBSERVED = "documented_not_observed"


class ProtocolEvidenceLevelV1(StrEnum):
    """Origin of support for a protocol claim, separate from its validation status."""

    ARTICLE_DOCUMENTED = "article_documented"
    STATIC_INFERENCE = "static_inference"
    SANDBOX_CAPTURE_OBSERVED = "sandbox_capture_observed"
    HYPOTHETICAL = "hypothetical"
    VERIFIED_ON_HOLDOUT = "verified_on_holdout"


class ProtocolValidationSplitV1(StrEnum):
    TRAIN = "train"
    HOLDOUT = "holdout"


class ProtocolValidationResultV1(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    PARTIAL = "partial"
    NOT_TESTABLE = "not_testable"


class ProtocolCharacteristicKindV1(StrEnum):
    FRAMING = "framing"
    MESSAGE_LENGTH = "message_length"
    ENCODING = "encoding"
    NEGOTIATION = "negotiation"
    CONFIGURATION = "configuration"
    TRANSFORMATION = "transformation"
    ENCRYPTION = "encryption"
    UNCERTAINTY = "uncertainty"


def _default_evidence_level(
    status: ProtocolValidationStatus,
    evidence: tuple[ProtocolEvidenceRefV1, ...] = (),
) -> ProtocolEvidenceLevelV1:
    if status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED:
        return ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
    if any(item.capture_sha256 is not None for item in evidence):
        return ProtocolEvidenceLevelV1.SANDBOX_CAPTURE_OBSERVED
    return ProtocolEvidenceLevelV1.STATIC_INFERENCE


def _validate_claim_evidence_level(
    level: ProtocolEvidenceLevelV1,
    status: ProtocolValidationStatus,
    evidence: tuple[ProtocolEvidenceRefV1, ...],
) -> None:
    all_documented = bool(evidence) and all(_is_documented_evidence(item) for item in evidence)
    all_captured = bool(evidence) and all(item.capture_sha256 is not None for item in evidence)
    if level is ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED:
        if status is not ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED or not all_documented:
            raise ValueError(
                "article-documented claims require documentary evidence and remain unobserved"
            )
    elif status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED:
        raise ValueError("documented claims must use the article_documented evidence level")
    elif level is ProtocolEvidenceLevelV1.STATIC_INFERENCE and not all_documented:
        raise ValueError("static inferences require cited static-analysis evidence")
    elif (
        level
        in {
            ProtocolEvidenceLevelV1.SANDBOX_CAPTURE_OBSERVED,
            ProtocolEvidenceLevelV1.HYPOTHETICAL,
            ProtocolEvidenceLevelV1.VERIFIED_ON_HOLDOUT,
        }
        and not all_captured
    ):
        raise ValueError("capture-derived claims require capture evidence references")
    if (
        level is ProtocolEvidenceLevelV1.VERIFIED_ON_HOLDOUT
        and status is not ProtocolValidationStatus.OBSERVED
    ):
        raise ValueError("verified-on-holdout claims must have a successful observed status")


class AssuranceLevel(StrEnum):
    CONFIRMED = "confirmed"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNCONFIRMED = "unconfirmed"


class FieldStability(StrEnum):
    STABLE = "stable"
    VARIANT = "variant"
    UNKNOWN = "unknown"


class ProtocolTestKind(StrEnum):
    MESSAGE_LENGTH = "message_length"
    FIELD_VALUE = "field_value"
    STATE_TRANSITION = "state_transition"
    DIRECTION = "direction"


class ProtocolDiagramKind(StrEnum):
    SEQUENCE = "sequence"
    STATE_MACHINE = "state_machine"
    MESSAGE_FORMAT = "message_format"


class ProtocolDiagramStatus(StrEnum):
    READY = "ready"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class _CanonicalContract:
    def to_dict(self) -> dict[str, Any]:
        value = _json_value(self)
        if not isinstance(value, dict):
            raise TypeError("contract must serialize to a JSON object")
        return value

    def canonical_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ProtocolEvidenceRefV1(_CanonicalContract):
    capture_sha256: str | None = None
    flow_id: str | None = None
    stream_id: str | None = None
    direction: StreamDirection | None = None
    frame_numbers: tuple[int, ...] = ()
    start_offset: int | None = None
    end_offset: int | None = None
    source: str = ""
    observation_id: UUID | None = None
    timestamp_start_ns: int | None = None
    timestamp_end_ns: int | None = None
    documented_ref_kind: str | None = None
    documented_ref_id: str | None = None
    documented_source_sha256: str | None = None

    def __post_init__(self) -> None:
        documented = self.documented_ref_kind is not None or self.documented_ref_id is not None
        if documented:
            if (
                self.documented_ref_kind not in {"observation", "finding", "claim"}
                or not self.documented_ref_id
                or not self.source
                or self.documented_source_sha256 is None
                or not _SHA256.fullmatch(self.documented_source_sha256)
            ):
                raise ValueError("documented evidence requires a typed reference and source hash")
            if (
                any(
                    value is not None
                    for value in (
                        self.capture_sha256,
                        self.flow_id,
                        self.stream_id,
                        self.direction,
                        self.start_offset,
                        self.end_offset,
                        self.observation_id,
                        self.timestamp_start_ns,
                        self.timestamp_end_ns,
                    )
                )
                or self.frame_numbers
            ):
                raise ValueError("documented evidence cannot contain capture or byte coordinates")
            if self.documented_ref_kind in {"observation", "finding"}:
                UUID(self.documented_ref_id)
            return
        if self.documented_source_sha256 is not None:
            raise ValueError("documented source hash requires a typed source reference")
        if not isinstance(self.capture_sha256, str) or not _SHA256.fullmatch(self.capture_sha256):
            raise ValueError("evidence capture sha256 must be lowercase hexadecimal")
        if not self.flow_id or not self.stream_id or not self.source or self.direction is None:
            raise ValueError("evidence flow, stream, direction, and source are required")
        if not self.frame_numbers and self.start_offset is None:
            raise ValueError("evidence must cite frames or a byte range")
        if any(isinstance(frame, bool) or frame < 1 for frame in self.frame_numbers):
            raise ValueError("evidence frame numbers must be positive")
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("evidence byte ranges require both offsets")
        if self.start_offset is not None and (
            self.start_offset < 0 or self.end_offset is None or self.end_offset <= self.start_offset
        ):
            raise ValueError("evidence byte range must be non-empty")
        if (self.timestamp_start_ns is None) != (self.timestamp_end_ns is None):
            raise ValueError("evidence timestamps require both bounds")
        if (
            self.timestamp_start_ns is not None
            and self.timestamp_end_ns is not None
            and self.timestamp_end_ns < self.timestamp_start_ns
        ):
            raise ValueError("evidence timestamp range is reversed")


@dataclass(frozen=True, slots=True)
class ProtocolEndpointV1(_CanonicalContract):
    address: str
    port: int | None
    channel: str | None
    evidence: tuple[ProtocolEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if not self.address:
            raise ValueError("endpoint address is required")
        if self.port is not None and not 0 <= self.port <= 65_535:
            raise ValueError("endpoint port is outside the valid range")


@dataclass(frozen=True, slots=True)
class ProtocolConversationV1(_CanonicalContract):
    capture_sha256: str
    report_id: UUID | None
    flow_id: str
    direction: StreamDirection
    source: ProtocolEndpointV1
    destination: ProtocolEndpointV1
    message_ids: tuple[str, ...]
    frame_numbers: tuple[int, ...]
    orientation_basis: str
    evidence: tuple[ProtocolEvidenceRefV1, ...]

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.capture_sha256):
            raise ValueError("conversation capture sha256 must be lowercase hexadecimal")
        if not self.flow_id or not self.orientation_basis:
            raise ValueError("conversation flow and orientation basis are required")


@dataclass(frozen=True, slots=True)
class MessageDefinitionV1(_CanonicalContract):
    message_id: str
    name: str
    ordinal: int
    directions: tuple[StreamDirection, ...]
    min_length: int | None
    max_length: int | None
    field_ids: tuple[str, ...]
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    encrypted_opaque: bool = False
    evidence_level: ProtocolEvidenceLevelV1 | None = None

    def __post_init__(self) -> None:
        if self.evidence_level is None:
            object.__setattr__(
                self,
                "evidence_level",
                _default_evidence_level(self.validation_status, self.evidence),
            )
        assert self.evidence_level is not None
        _validate_claim_evidence_level(self.evidence_level, self.validation_status, self.evidence)
        if not self.message_id or not self.name or self.ordinal < 1:
            raise ValueError("message identity and positive ordinal are required")
        if (self.min_length is None) != (self.max_length is None):
            raise ValueError("message length bounds must be both known or both unknown")
        if self.min_length is not None and (
            self.max_length is None or self.min_length < 0 or self.max_length < self.min_length
        ):
            raise ValueError("message length range is invalid")
        if self.validation_status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED and (
            self.min_length is not None or self.max_length is not None
        ):
            raise ValueError("documented messages cannot claim observed byte lengths")
        if not self.evidence:
            raise ValueError("each message definition requires evidence")


@dataclass(frozen=True, slots=True)
class ProtocolStateV1(_CanonicalContract):
    state_id: str
    name: str
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    evidence_level: ProtocolEvidenceLevelV1 | None = None

    def __post_init__(self) -> None:
        if self.evidence_level is None:
            object.__setattr__(
                self,
                "evidence_level",
                _default_evidence_level(self.validation_status, self.evidence),
            )
        assert self.evidence_level is not None
        _validate_claim_evidence_level(self.evidence_level, self.validation_status, self.evidence)
        if not self.state_id or not self.name or not self.evidence:
            raise ValueError("protocol states require an identity, name, and cited evidence")


@dataclass(frozen=True, slots=True)
class FieldDefinitionV1(_CanonicalContract):
    field_id: str
    message_id: str
    name: str
    offset: int | None
    length: int | None
    endianness: str | None
    encoding: str | None
    condition: str | None
    source: str
    validation_status: ProtocolValidationStatus
    stability: FieldStability
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    value_sha256s: tuple[str, ...] = ()
    expected_hex: str | None = None
    encrypted_opaque: bool = False
    justification: str | None = None
    evidence_level: ProtocolEvidenceLevelV1 | None = None

    def __post_init__(self) -> None:
        if self.evidence_level is None:
            object.__setattr__(
                self,
                "evidence_level",
                _default_evidence_level(self.validation_status, self.evidence),
            )
        assert self.evidence_level is not None
        _validate_claim_evidence_level(self.evidence_level, self.validation_status, self.evidence)
        if not self.field_id or not self.message_id or not self.name or not self.source:
            raise ValueError("field identity and source are required")
        if self.offset is not None and self.offset < 0:
            raise ValueError("field offset cannot be negative")
        if self.length is not None and self.length < 1:
            raise ValueError("field length must be positive")
        if self.endianness not in {None, "big", "little", "none", "unknown"}:
            raise ValueError("field endianness is invalid")
        if not self.evidence:
            raise ValueError("each field definition requires evidence")
        if any(not _SHA256.fullmatch(value) for value in self.value_sha256s):
            raise ValueError("field value digests must be lowercase sha256")
        if self.expected_hex is not None and not _SHORT_HEX.fullmatch(self.expected_hex):
            raise ValueError("expected_hex is limited to at most 16 bytes")
        if (
            self.expected_hex is not None
            and self.length is not None
            and len(self.expected_hex) != self.length * 2
        ):
            raise ValueError("expected_hex length must match the field length")
        if self.encrypted_opaque and self.expected_hex is not None:
            raise ValueError("opaque encrypted fields cannot have decoded expected values")
        if self.validation_status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED and (
            self.offset is not None
            or self.length is not None
            or self.expected_hex is not None
            or self.value_sha256s
            or self.endianness not in {None, "unknown"}
        ):
            raise ValueError("documented fields cannot claim byte coordinates or observed values")


@dataclass(frozen=True, slots=True)
class ProtocolTransitionV1(_CanonicalContract):
    transition_id: str
    from_state: str
    to_state: str
    message_id: str
    trigger: str
    hypotheses: tuple[str, ...]
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    evidence_level: ProtocolEvidenceLevelV1 | None = None

    def __post_init__(self) -> None:
        if self.evidence_level is None:
            object.__setattr__(
                self,
                "evidence_level",
                _default_evidence_level(self.validation_status, self.evidence),
            )
        assert self.evidence_level is not None
        _validate_claim_evidence_level(self.evidence_level, self.validation_status, self.evidence)
        if not all(
            (self.transition_id, self.from_state, self.to_state, self.message_id, self.trigger)
        ):
            raise ValueError("transition identity, states, message, and trigger are required")
        if not self.evidence:
            raise ValueError("each transition requires evidence")


@dataclass(frozen=True, slots=True)
class BinaryFunctionProvenanceV1(_CanonicalContract):
    sample_sha256: str
    binary_sha256: str
    provider: str
    provider_version: str
    function_id: str
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    execution_mode: str = "static_non_executed"
    evidence_level: ProtocolEvidenceLevelV1 = ProtocolEvidenceLevelV1.STATIC_INFERENCE

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sample_sha256) or not _SHA256.fullmatch(self.binary_sha256):
            raise ValueError("binary provenance digests must be lowercase sha256")
        if not all((self.provider, self.provider_version, self.function_id)) or not self.evidence:
            raise ValueError("function provenance requires provider, function, and evidence")
        if self.execution_mode != "static_non_executed":
            raise ValueError("protocol binary provenance must remain static and non-executed")
        if self.evidence_level is not ProtocolEvidenceLevelV1.STATIC_INFERENCE:
            raise ValueError("binary function provenance must remain a static inference")


@dataclass(frozen=True, slots=True)
class ProtocolCharacteristicV1(_CanonicalContract):
    """Evidence-bound framing, negotiation, configuration, or transformation claim."""

    kind: ProtocolCharacteristicKindV1
    name: str
    statement: str
    evidence_level: ProtocolEvidenceLevelV1
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    justification: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.name, self.statement, self.justification)
        ):
            raise ValueError("protocol characteristics require a claim and justification")
        if len(self.name) > 128 or len(self.statement) > 1024 or len(self.justification) > 1024:
            raise ValueError("protocol characteristic text exceeds its bound")
        if not self.evidence:
            raise ValueError("protocol characteristics require evidence references")
        _validate_claim_evidence_level(self.evidence_level, self.validation_status, self.evidence)


@dataclass(frozen=True, slots=True)
class ProtocolValidationRunV1(_CanonicalContract):
    """Result for one bounded training or holdout pass over cited traces."""

    split: ProtocolValidationSplitV1
    result: ProtocolValidationResultV1
    tested_item_ids: tuple[str, ...]
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    detail: str
    evidence_level: ProtocolEvidenceLevelV1 | None = None

    def __post_init__(self) -> None:
        expected_level = _validation_run_evidence_level(self.split, self.result, self.evidence)
        if self.evidence_level is None:
            object.__setattr__(self, "evidence_level", expected_level)
        elif self.evidence_level is not expected_level:
            raise ValueError("validation evidence level must match its dataset split")
        if len(set(self.tested_item_ids)) != len(self.tested_item_ids) or any(
            not item for item in self.tested_item_ids
        ):
            raise ValueError("validation run item references must be unique and non-empty")
        if len(self.detail) > 1024:
            raise ValueError("validation run detail exceeds its bound")
        if self.result is ProtocolValidationResultV1.NOT_TESTABLE and self.tested_item_ids:
            raise ValueError("not-testable validation runs cannot claim tested items")
        if self.result in {
            ProtocolValidationResultV1.VERIFIED,
            ProtocolValidationResultV1.REJECTED,
            ProtocolValidationResultV1.PARTIAL,
        } and (not self.tested_item_ids or not self.evidence):
            raise ValueError("tested validation runs require tested items and evidence")


def _validation_run_evidence_level(
    split: ProtocolValidationSplitV1,
    result: ProtocolValidationResultV1,
    evidence: tuple[ProtocolEvidenceRefV1, ...],
) -> ProtocolEvidenceLevelV1:
    all_captured = bool(evidence) and all(item.capture_sha256 is not None for item in evidence)
    if split is ProtocolValidationSplitV1.TRAIN:
        if (
            result
            in {
                ProtocolValidationResultV1.VERIFIED,
                ProtocolValidationResultV1.REJECTED,
                ProtocolValidationResultV1.PARTIAL,
            }
            and not all_captured
        ):
            raise ValueError("tested train results require capture evidence")
        return (
            ProtocolEvidenceLevelV1.HYPOTHETICAL
            if all_captured
            else ProtocolEvidenceLevelV1.STATIC_INFERENCE
        )
    if result is ProtocolValidationResultV1.VERIFIED:
        if not all_captured:
            raise ValueError("holdout verification requires capture evidence")
        return ProtocolEvidenceLevelV1.VERIFIED_ON_HOLDOUT
    if (
        result
        in {
            ProtocolValidationResultV1.REJECTED,
            ProtocolValidationResultV1.PARTIAL,
        }
        and not all_captured
    ):
        raise ValueError("tested holdout results require capture evidence")
    if all_captured:
        return ProtocolEvidenceLevelV1.SANDBOX_CAPTURE_OBSERVED
    return ProtocolEvidenceLevelV1.STATIC_INFERENCE


@dataclass(frozen=True, slots=True)
class ProtocolVerificationFindingV1(_CanonicalContract):
    finding_id: str
    item_id: str
    kind: str
    validation_status: ProtocolValidationStatus
    expected: str | None
    observed: str | None
    detail: str
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    evidence_level: ProtocolEvidenceLevelV1 | None = None

    def __post_init__(self) -> None:
        if self.evidence_level is None:
            object.__setattr__(
                self,
                "evidence_level",
                _default_evidence_level(self.validation_status, self.evidence),
            )
        assert self.evidence_level is not None
        _validate_claim_evidence_level(self.evidence_level, self.validation_status, self.evidence)
        if self.validation_status not in {
            ProtocolValidationStatus.CONTRADICTED,
            ProtocolValidationStatus.UNRESOLVED,
        }:
            raise ValueError("verification findings must preserve a contradiction or gap")
        if not self.finding_id or not self.item_id or not self.kind or not self.evidence:
            raise ValueError("verification finding identity and evidence are required")


@dataclass(frozen=True, slots=True)
class ProtocolTestPlanStepV1(_CanonicalContract):
    kind: ProtocolTestKind
    message_id: str
    field_id: str | None = None
    transition_id: str | None = None

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("test plan steps require a message id")
        if self.kind is ProtocolTestKind.FIELD_VALUE and not self.field_id:
            raise ValueError("field_value test steps require a field id")
        if self.kind is ProtocolTestKind.STATE_TRANSITION and not self.transition_id:
            raise ValueError("state_transition test steps require a transition id")


@dataclass(frozen=True, slots=True)
class ProtocolModelV1(_CanonicalContract):
    schema_version: str
    subject_id: str
    investigation_id: UUID
    family: str | None
    cluster: str | None
    family_assurance: AssuranceLevel
    sample_sha256s: tuple[str, ...]
    capture_sha256s: tuple[str, ...]
    report_ids: tuple[UUID, ...]
    model_version: str
    protocol: str | None
    transport: NetworkTransport
    endpoints: tuple[ProtocolEndpointV1, ...]
    client_server_orientation: str
    infection_state: str | None
    conversations: tuple[ProtocolConversationV1, ...]
    messages: tuple[MessageDefinitionV1, ...]
    fields: tuple[FieldDefinitionV1, ...]
    transitions: tuple[ProtocolTransitionV1, ...]
    function_provenance: tuple[BinaryFunctionProvenanceV1, ...]
    validation_status: ProtocolValidationStatus
    verification_findings: tuple[ProtocolVerificationFindingV1, ...]
    test_plan: tuple[ProtocolTestPlanStepV1, ...]
    limits: NetworkExtractionLimitsV1
    limitations: tuple[str, ...]
    extractor_versions: tuple[tuple[str, str], ...]
    states: tuple[ProtocolStateV1, ...] = ()
    documentary_evidence: tuple[ProtocolEvidenceRefV1, ...] = ()
    characteristics: tuple[ProtocolCharacteristicV1, ...] = ()
    validation_runs: tuple[ProtocolValidationRunV1, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != "ProtocolModelV1":
            raise ValueError("unsupported protocol model schema")
        if not self.subject_id.strip() or not self.model_version.strip():
            raise ValueError("subject id and model version are required")
        if not self.client_server_orientation:
            raise ValueError("client/server orientation must be explicit, including unresolved")
        if any(
            not _SHA256.fullmatch(value) for value in self.sample_sha256s + self.capture_sha256s
        ):
            raise ValueError("model sample and capture hashes must be lowercase sha256")
        message_ids = {item.message_id for item in self.messages}
        field_ids = {item.field_id for item in self.fields}
        transition_ids = {item.transition_id for item in self.transitions}
        message_keys = {
            (direction, message.ordinal)
            for message in self.messages
            for direction in message.directions
        }
        if len(message_ids) != len(self.messages) or len(field_ids) != len(self.fields):
            raise ValueError("model message and field ids must be unique")
        if len(message_keys) != sum(len(message.directions) for message in self.messages):
            raise ValueError("message direction and ordinal pairs must be unique")
        if len(transition_ids) != len(self.transitions):
            raise ValueError("model transition ids must be unique")
        if any(item.message_id not in message_ids for item in self.fields):
            raise ValueError("field references an unknown message")
        if any(item.message_id not in message_ids for item in self.transitions):
            raise ValueError("transition references an unknown message")
        if any(
            field_id not in field_ids for message in self.messages for field_id in message.field_ids
        ):
            raise ValueError("message references an unknown field")
        state_ids = {item.state_id for item in self.states}
        if len(state_ids) != len(self.states):
            raise ValueError("protocol state ids must be unique")
        if self.validation_status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED and any(
            transition.from_state not in state_ids or transition.to_state not in state_ids
            for transition in self.transitions
        ):
            raise ValueError("documented transitions must cite explicitly documented states")
        if self.validation_status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED:
            if self.capture_sha256s or self.conversations:
                raise ValueError("documentary models cannot contain network captures")
            if any(
                item.execution_mode != "static_non_executed"
                or item.evidence_level is not ProtocolEvidenceLevelV1.STATIC_INFERENCE
                for item in self.function_provenance
            ):
                raise ValueError(
                    "documentary function references must remain static and unexecuted"
                )
            if not self.documentary_evidence or not all(
                _is_documented_evidence(ref) for ref in self.documentary_evidence
            ):
                raise ValueError("documentary models require cited source evidence")
            if any(
                getattr(item, "validation_status", None)
                is not ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED
                or not all(_is_documented_evidence(ref) for ref in getattr(item, "evidence", ()))
                for item in (*self.messages, *self.states, *self.fields, *self.transitions)
            ):
                raise ValueError(
                    "every documentary protocol element requires cited documentary evidence"
                )
            if any(
                item.offset is not None
                or item.length is not None
                or item.expected_hex is not None
                or item.value_sha256s
                for item in self.fields
            ):
                raise ValueError("documentary models cannot contain byte offsets or values")
            if any(
                item.min_length is not None or item.max_length is not None for item in self.messages
            ):
                raise ValueError("documentary models cannot contain observed message lengths")
            if any(
                getattr(item, "evidence_level", None)
                is not ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
                for item in (*self.messages, *self.states, *self.fields, *self.transitions)
            ):
                raise ValueError("documentary elements must retain the article evidence level")
            if any(
                item.evidence_level is not ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED
                for item in self.characteristics
            ):
                raise ValueError("documentary characteristics must remain explicitly unobserved")

    @classmethod
    def from_dict(cls, value: object) -> ProtocolModelV1:
        data = _strict_object(
            value,
            {
                "schema_version",
                "subject_id",
                "investigation_id",
                "family",
                "cluster",
                "family_assurance",
                "sample_sha256s",
                "capture_sha256s",
                "report_ids",
                "model_version",
                "protocol",
                "transport",
                "endpoints",
                "client_server_orientation",
                "infection_state",
                "conversations",
                "messages",
                "fields",
                "transitions",
                "function_provenance",
                "validation_status",
                "verification_findings",
                "test_plan",
                "limits",
                "limitations",
                "extractor_versions",
                "states",
                "documentary_evidence",
                "characteristics",
                "validation_runs",
            },
            optional_keys={
                "states",
                "documentary_evidence",
                "characteristics",
                "validation_runs",
            },
        )
        return cls(
            schema_version=_str(data["schema_version"]),
            subject_id=_str(data["subject_id"]),
            investigation_id=UUID(_str(data["investigation_id"])),
            family=_optional_str(data["family"]),
            cluster=_optional_str(data["cluster"]),
            family_assurance=AssuranceLevel(_str(data["family_assurance"])),
            sample_sha256s=_str_tuple(data["sample_sha256s"]),
            capture_sha256s=_str_tuple(data["capture_sha256s"]),
            report_ids=tuple(UUID(_str(item)) for item in _list(data["report_ids"])),
            model_version=_str(data["model_version"]),
            protocol=_optional_str(data["protocol"]),
            transport=NetworkTransport(_str(data["transport"])),
            endpoints=tuple(_endpoint_from_dict(item) for item in _list(data["endpoints"])),
            client_server_orientation=_str(data["client_server_orientation"]),
            infection_state=_optional_str(data["infection_state"]),
            conversations=tuple(
                _conversation_from_dict(item) for item in _list(data["conversations"])
            ),
            messages=tuple(_message_from_dict(item) for item in _list(data["messages"])),
            fields=tuple(_field_from_dict(item) for item in _list(data["fields"])),
            transitions=tuple(_transition_from_dict(item) for item in _list(data["transitions"])),
            function_provenance=tuple(
                _function_from_dict(item) for item in _list(data["function_provenance"])
            ),
            validation_status=ProtocolValidationStatus(_str(data["validation_status"])),
            verification_findings=tuple(
                _finding_from_dict(item) for item in _list(data["verification_findings"])
            ),
            test_plan=tuple(_test_step_from_dict(item) for item in _list(data["test_plan"])),
            limits=_limits_from_dict(data["limits"]),
            limitations=_str_tuple(data["limitations"]),
            extractor_versions=_versions_tuple(data["extractor_versions"]),
            states=tuple(_state_from_dict(item) for item in _list(data.get("states", []))),
            documentary_evidence=_evidence_tuple(data.get("documentary_evidence", [])),
            characteristics=tuple(
                _characteristic_from_dict(item) for item in _list(data.get("characteristics", []))
            ),
            validation_runs=tuple(
                _validation_run_from_dict(item) for item in _list(data.get("validation_runs", []))
            ),
        )


@dataclass(frozen=True, slots=True)
class HypothesisMessageV1(_CanonicalContract):
    message_id: str
    name: str
    ordinal: int
    direction: StreamDirection
    min_length: int
    max_length: int
    encrypted_opaque: bool = False

    def __post_init__(self) -> None:
        if not self.message_id or not self.name or self.ordinal < 1:
            raise ValueError("hypothesis message identity and ordinal are required")
        if self.min_length < 0 or self.max_length < self.min_length:
            raise ValueError("hypothesis message length range is invalid")


@dataclass(frozen=True, slots=True)
class HypothesisFieldV1(_CanonicalContract):
    field_id: str
    message_id: str
    name: str
    offset: int
    length: int
    endianness: str | None
    encoding: str | None
    condition: str | None
    expected_hex: str | None
    encrypted_opaque: bool = False
    justification: str | None = None

    def __post_init__(self) -> None:
        if not self.field_id or not self.message_id or not self.name:
            raise ValueError("hypothesis field identity is required")
        if self.offset < 0 or self.length < 1:
            raise ValueError("hypothesis field range is invalid")
        if self.endianness not in {None, "big", "little", "none", "unknown"}:
            raise ValueError("hypothesis field endianness is invalid")
        if self.expected_hex is not None and not _SHORT_HEX.fullmatch(self.expected_hex):
            raise ValueError("hypothesis expected_hex is limited to at most 16 bytes")
        if self.expected_hex is not None and len(self.expected_hex) != self.length * 2:
            raise ValueError("hypothesis expected_hex length must match the field length")
        if self.encrypted_opaque and self.expected_hex is not None:
            raise ValueError("opaque encrypted fields cannot have decoded expected values")


@dataclass(frozen=True, slots=True)
class HypothesisTransitionV1(_CanonicalContract):
    transition_id: str
    from_state: str
    to_state: str
    message_id: str
    trigger: str

    def __post_init__(self) -> None:
        if not all(
            (self.transition_id, self.from_state, self.to_state, self.message_id, self.trigger)
        ):
            raise ValueError("hypothesis transition fields are required")


@dataclass(frozen=True, slots=True)
class HypothesisCharacteristicV1(_CanonicalContract):
    kind: ProtocolCharacteristicKindV1
    name: str
    statement: str
    justification: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.name, self.statement, self.justification)
        ):
            raise ValueError("hypothesis characteristics require a claim and rationale")
        if any(len(value) > 1024 for value in (self.name, self.statement, self.justification)):
            raise ValueError("hypothesis characteristic text exceeds its bound")


@dataclass(frozen=True, slots=True)
class ProtocolHypothesisProposalV1(_CanonicalContract):
    schema_version: str
    messages: tuple[HypothesisMessageV1, ...]
    fields: tuple[HypothesisFieldV1, ...]
    transitions: tuple[HypothesisTransitionV1, ...]
    test_plan: tuple[ProtocolTestPlanStepV1, ...]
    characteristics: tuple[HypothesisCharacteristicV1, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != "ProtocolHypothesisProposalV1":
            raise ValueError("unsupported hypothesis proposal schema")
        message_ids = {item.message_id for item in self.messages}
        if not self.messages or len(message_ids) != len(self.messages):
            raise ValueError("hypothesis requires unique message definitions")
        field_ids = {item.field_id for item in self.fields}
        if len(field_ids) != len(self.fields):
            raise ValueError("hypothesis field ids must be unique")
        if any(item.message_id not in message_ids for item in self.fields + self.transitions):
            raise ValueError("hypothesis field or transition references an unknown message")
        if any(step.message_id not in message_ids for step in self.test_plan):
            raise ValueError("hypothesis test plan references an unknown message")
        field_ids = {item.field_id for item in self.fields}
        transition_ids = {item.transition_id for item in self.transitions}
        for step in self.test_plan:
            if step.kind is ProtocolTestKind.FIELD_VALUE and step.field_id not in field_ids:
                raise ValueError("hypothesis field test references an unknown field")
            if (
                step.kind is ProtocolTestKind.STATE_TRANSITION
                and step.transition_id not in transition_ids
            ):
                raise ValueError("hypothesis transition test references an unknown transition")

    @classmethod
    def from_dict(cls, value: object) -> ProtocolHypothesisProposalV1:
        data = _strict_object(
            value,
            {
                "schema_version",
                "messages",
                "fields",
                "transitions",
                "test_plan",
                "characteristics",
            },
            optional_keys={"characteristics"},
        )
        return cls(
            schema_version=_str(data["schema_version"]),
            messages=tuple(_hypothesis_message_from_dict(item) for item in _list(data["messages"])),
            fields=tuple(_hypothesis_field_from_dict(item) for item in _list(data["fields"])),
            transitions=tuple(
                _hypothesis_transition_from_dict(item) for item in _list(data["transitions"])
            ),
            test_plan=tuple(_test_step_from_dict(item) for item in _list(data["test_plan"])),
            characteristics=tuple(
                _hypothesis_characteristic_from_dict(item)
                for item in _list(data.get("characteristics", []))
            ),
        )


@dataclass(frozen=True, slots=True)
class ProtocolDiagramNodeV1(_CanonicalContract):
    node_id: str
    label: str
    role: str
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...] = ()
    evidence_level: ProtocolEvidenceLevelV1 | None = None


@dataclass(frozen=True, slots=True)
class ProtocolDiagramEdgeV1(_CanonicalContract):
    source_id: str
    target_id: str
    label: str
    order: int | None
    frame_numbers: tuple[int, ...]
    timing_ms: int | None
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    evidence_level: ProtocolEvidenceLevelV1 | None = None


@dataclass(frozen=True, slots=True)
class ProtocolDiagramFieldV1(_CanonicalContract):
    field_id: str
    name: str
    offset: int | None
    length: int | None
    endianness: str | None
    encoding: str | None
    condition: str | None
    encrypted_opaque: bool
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    evidence_level: ProtocolEvidenceLevelV1 | None = None
    justification: str | None = None


@dataclass(frozen=True, slots=True)
class ProtocolDiagramSpecV1(_CanonicalContract):
    schema_version: str
    kind: ProtocolDiagramKind
    status: ProtocolDiagramStatus
    title: str
    nodes: tuple[ProtocolDiagramNodeV1, ...]
    edges: tuple[ProtocolDiagramEdgeV1, ...]
    fields: tuple[ProtocolDiagramFieldV1, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "ProtocolDiagramSpecV1":
            raise ValueError("unsupported protocol diagram schema")
        if not self.title:
            raise ValueError("diagram title is required")
        if self.status is ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE and (
            self.nodes or self.edges or self.fields
        ):
            raise ValueError("insufficient-evidence specs cannot contain a generic scenario")


@dataclass(frozen=True, slots=True)
class ProtocolRenderResultV1(_CanonicalContract):
    schema_version: str
    model_sha256: str
    diagrams: tuple[ProtocolDiagramSpecV1, ...]
    factual_statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "ProtocolRenderResultV1":
            raise ValueError("unsupported render result schema")
        if not _SHA256.fullmatch(self.model_sha256):
            raise ValueError("render result model digest must be lowercase sha256")
        if {item.kind for item in self.diagrams} != set(ProtocolDiagramKind):
            raise ValueError("render result must provide exactly the three diagram kinds")


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _strict_object(
    value: object,
    keys: set[str],
    *,
    optional_keys: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not optional_keys.issubset(keys)
        or not (keys - optional_keys).issubset(value)
        or set(value) - keys
    ):
        raise ValueError("contract object has missing or unknown fields")
    return value


def _is_documented_evidence(value: ProtocolEvidenceRefV1) -> bool:
    return (
        value.documented_ref_kind in {"observation", "finding", "claim"}
        and value.documented_ref_id is not None
        and value.documented_source_sha256 is not None
        and value.capture_sha256 is None
        and not value.frame_numbers
        and value.start_offset is None
        and value.end_offset is None
    )


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("contract string field is invalid")
    return value


def _optional_str(value: object) -> str | None:
    return None if value is None else _str(value)


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("contract array field is invalid")
    return value


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("contract integer field is invalid")
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("contract boolean field is invalid")
    return value


def _str_tuple(value: object) -> tuple[str, ...]:
    return tuple(_str(item) for item in _list(value))


def _versions_tuple(value: object) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for item in _list(value):
        pair = _list(item)
        if len(pair) != 2:
            raise ValueError("extractor version entries require a name and version")
        result.append((_str(pair[0]), _str(pair[1])))
    return tuple(result)


def _limits_from_dict(value: object) -> NetworkExtractionLimitsV1:
    names = {
        "max_capture_bytes",
        "max_packets",
        "max_output_bytes",
        "max_flows",
        "max_endpoints",
        "max_protocols",
        "max_stream_bytes",
        "max_messages",
        "max_field_observations",
        "max_field_value_chars",
        "max_frame_refs_per_record",
        "max_detail_ranges",
        "max_preview_bytes",
    }
    data = _strict_object(value, names)
    return NetworkExtractionLimitsV1(**{name: _int(data[name]) for name in names})


def _evidence_from_dict(value: object) -> ProtocolEvidenceRefV1:
    required = {
        "capture_sha256",
        "flow_id",
        "stream_id",
        "direction",
        "frame_numbers",
        "start_offset",
        "end_offset",
        "source",
        "observation_id",
        "timestamp_start_ns",
        "timestamp_end_ns",
    }
    documentary = {
        "documented_ref_kind",
        "documented_ref_id",
        "documented_source_sha256",
    }
    data = _strict_object(
        value,
        required | documentary,
        optional_keys=documentary,
    )
    frames = _list(data["frame_numbers"])
    return ProtocolEvidenceRefV1(
        capture_sha256=_optional_str(data["capture_sha256"]),
        flow_id=_optional_str(data["flow_id"]),
        stream_id=_optional_str(data["stream_id"]),
        direction=None if data["direction"] is None else StreamDirection(_str(data["direction"])),
        frame_numbers=tuple(_int(item) for item in frames),
        start_offset=None if data["start_offset"] is None else _int(data["start_offset"]),
        end_offset=None if data["end_offset"] is None else _int(data["end_offset"]),
        source=_str(data["source"]),
        observation_id=None
        if data["observation_id"] is None
        else UUID(_str(data["observation_id"])),
        timestamp_start_ns=(
            None if data["timestamp_start_ns"] is None else _int(data["timestamp_start_ns"])
        ),
        timestamp_end_ns=(
            None if data["timestamp_end_ns"] is None else _int(data["timestamp_end_ns"])
        ),
        documented_ref_kind=_optional_str(data.get("documented_ref_kind")),
        documented_ref_id=_optional_str(data.get("documented_ref_id")),
        documented_source_sha256=_optional_str(data.get("documented_source_sha256")),
    )


def _evidence_tuple(value: object) -> tuple[ProtocolEvidenceRefV1, ...]:
    return tuple(_evidence_from_dict(item) for item in _list(value))


def _endpoint_from_dict(value: object) -> ProtocolEndpointV1:
    data = _strict_object(value, {"address", "port", "channel", "evidence"})
    return ProtocolEndpointV1(
        address=_str(data["address"]),
        port=None if data["port"] is None else _int(data["port"]),
        channel=_optional_str(data["channel"]),
        evidence=_evidence_tuple(data["evidence"]),
    )


def _conversation_from_dict(value: object) -> ProtocolConversationV1:
    data = _strict_object(
        value,
        {
            "capture_sha256",
            "report_id",
            "flow_id",
            "direction",
            "source",
            "destination",
            "message_ids",
            "frame_numbers",
            "orientation_basis",
            "evidence",
        },
    )
    return ProtocolConversationV1(
        capture_sha256=_str(data["capture_sha256"]),
        report_id=None if data["report_id"] is None else UUID(_str(data["report_id"])),
        flow_id=_str(data["flow_id"]),
        direction=StreamDirection(_str(data["direction"])),
        source=_endpoint_from_dict(data["source"]),
        destination=_endpoint_from_dict(data["destination"]),
        message_ids=_str_tuple(data["message_ids"]),
        frame_numbers=tuple(_int(item) for item in _list(data["frame_numbers"])),
        orientation_basis=_str(data["orientation_basis"]),
        evidence=_evidence_tuple(data["evidence"]),
    )


def _message_from_dict(value: object) -> MessageDefinitionV1:
    data = _strict_object(
        value,
        {
            "message_id",
            "name",
            "ordinal",
            "directions",
            "min_length",
            "max_length",
            "field_ids",
            "validation_status",
            "evidence",
            "encrypted_opaque",
            "evidence_level",
        },
        optional_keys={"evidence_level"},
    )
    status = ProtocolValidationStatus(_str(data["validation_status"]))
    return MessageDefinitionV1(
        message_id=_str(data["message_id"]),
        name=_str(data["name"]),
        ordinal=_int(data["ordinal"]),
        directions=tuple(StreamDirection(_str(item)) for item in _list(data["directions"])),
        min_length=None if data["min_length"] is None else _int(data["min_length"]),
        max_length=None if data["max_length"] is None else _int(data["max_length"]),
        field_ids=_str_tuple(data["field_ids"]),
        validation_status=status,
        evidence=_evidence_tuple(data["evidence"]),
        encrypted_opaque=_bool(data["encrypted_opaque"]),
        evidence_level=_evidence_level(
            data.get("evidence_level"), status, _evidence_tuple(data["evidence"])
        ),
    )


def _state_from_dict(value: object) -> ProtocolStateV1:
    data = _strict_object(
        value,
        {"state_id", "name", "validation_status", "evidence", "evidence_level"},
        optional_keys={"evidence_level"},
    )
    status = ProtocolValidationStatus(_str(data["validation_status"]))
    return ProtocolStateV1(
        state_id=_str(data["state_id"]),
        name=_str(data["name"]),
        validation_status=status,
        evidence=_evidence_tuple(data["evidence"]),
        evidence_level=_evidence_level(
            data.get("evidence_level"), status, _evidence_tuple(data["evidence"])
        ),
    )


def _field_from_dict(value: object) -> FieldDefinitionV1:
    data = _strict_object(
        value,
        {
            "field_id",
            "message_id",
            "name",
            "offset",
            "length",
            "endianness",
            "encoding",
            "condition",
            "source",
            "validation_status",
            "stability",
            "evidence",
            "value_sha256s",
            "expected_hex",
            "encrypted_opaque",
            "justification",
            "evidence_level",
        },
        optional_keys={"justification", "evidence_level"},
    )
    status = ProtocolValidationStatus(_str(data["validation_status"]))
    return FieldDefinitionV1(
        field_id=_str(data["field_id"]),
        message_id=_str(data["message_id"]),
        name=_str(data["name"]),
        offset=None if data["offset"] is None else _int(data["offset"]),
        length=None if data["length"] is None else _int(data["length"]),
        endianness=_optional_str(data["endianness"]),
        encoding=_optional_str(data["encoding"]),
        condition=_optional_str(data["condition"]),
        source=_str(data["source"]),
        validation_status=status,
        stability=FieldStability(_str(data["stability"])),
        evidence=_evidence_tuple(data["evidence"]),
        value_sha256s=_str_tuple(data["value_sha256s"]),
        expected_hex=_optional_str(data["expected_hex"]),
        encrypted_opaque=_bool(data["encrypted_opaque"]),
        justification=_optional_str(data.get("justification")),
        evidence_level=_evidence_level(
            data.get("evidence_level"), status, _evidence_tuple(data["evidence"])
        ),
    )


def _transition_from_dict(value: object) -> ProtocolTransitionV1:
    data = _strict_object(
        value,
        {
            "transition_id",
            "from_state",
            "to_state",
            "message_id",
            "trigger",
            "hypotheses",
            "validation_status",
            "evidence",
            "evidence_level",
        },
        optional_keys={"evidence_level"},
    )
    status = ProtocolValidationStatus(_str(data["validation_status"]))
    return ProtocolTransitionV1(
        transition_id=_str(data["transition_id"]),
        from_state=_str(data["from_state"]),
        to_state=_str(data["to_state"]),
        message_id=_str(data["message_id"]),
        trigger=_str(data["trigger"]),
        hypotheses=_str_tuple(data["hypotheses"]),
        validation_status=status,
        evidence=_evidence_tuple(data["evidence"]),
        evidence_level=_evidence_level(
            data.get("evidence_level"), status, _evidence_tuple(data["evidence"])
        ),
    )


def _function_from_dict(value: object) -> BinaryFunctionProvenanceV1:
    data = _strict_object(
        value,
        {
            "sample_sha256",
            "binary_sha256",
            "provider",
            "provider_version",
            "function_id",
            "evidence",
            "execution_mode",
            "evidence_level",
        },
        optional_keys={"execution_mode", "evidence_level"},
    )
    return BinaryFunctionProvenanceV1(
        sample_sha256=_str(data["sample_sha256"]),
        binary_sha256=_str(data["binary_sha256"]),
        provider=_str(data["provider"]),
        provider_version=_str(data["provider_version"]),
        function_id=_str(data["function_id"]),
        evidence=_evidence_tuple(data["evidence"]),
        execution_mode=_str(data.get("execution_mode", "static_non_executed")),
        evidence_level=ProtocolEvidenceLevelV1(
            _str(data.get("evidence_level", ProtocolEvidenceLevelV1.STATIC_INFERENCE.value))
        ),
    )


def _finding_from_dict(value: object) -> ProtocolVerificationFindingV1:
    data = _strict_object(
        value,
        {
            "finding_id",
            "item_id",
            "kind",
            "validation_status",
            "expected",
            "observed",
            "detail",
            "evidence",
            "evidence_level",
        },
        optional_keys={"evidence_level"},
    )
    status = ProtocolValidationStatus(_str(data["validation_status"]))
    evidence = _evidence_tuple(data["evidence"])
    return ProtocolVerificationFindingV1(
        finding_id=_str(data["finding_id"]),
        item_id=_str(data["item_id"]),
        kind=_str(data["kind"]),
        validation_status=status,
        expected=_optional_str(data["expected"]),
        observed=_optional_str(data["observed"]),
        detail=_str(data["detail"]),
        evidence=evidence,
        evidence_level=_evidence_level(data.get("evidence_level"), status, evidence),
    )


def _test_step_from_dict(value: object) -> ProtocolTestPlanStepV1:
    data = _strict_object(value, {"kind", "message_id", "field_id", "transition_id"})
    return ProtocolTestPlanStepV1(
        kind=ProtocolTestKind(_str(data["kind"])),
        message_id=_str(data["message_id"]),
        field_id=_optional_str(data["field_id"]),
        transition_id=_optional_str(data["transition_id"]),
    )


def _characteristic_from_dict(value: object) -> ProtocolCharacteristicV1:
    data = _strict_object(
        value,
        {
            "kind",
            "name",
            "statement",
            "evidence_level",
            "validation_status",
            "evidence",
            "justification",
        },
    )
    return ProtocolCharacteristicV1(
        kind=ProtocolCharacteristicKindV1(_str(data["kind"])),
        name=_str(data["name"]),
        statement=_str(data["statement"]),
        evidence_level=ProtocolEvidenceLevelV1(_str(data["evidence_level"])),
        validation_status=ProtocolValidationStatus(_str(data["validation_status"])),
        evidence=_evidence_tuple(data["evidence"]),
        justification=_str(data["justification"]),
    )


def _validation_run_from_dict(value: object) -> ProtocolValidationRunV1:
    data = _strict_object(
        value,
        {"split", "result", "tested_item_ids", "evidence", "detail", "evidence_level"},
        optional_keys={"evidence_level"},
    )
    return ProtocolValidationRunV1(
        split=ProtocolValidationSplitV1(_str(data["split"])),
        result=ProtocolValidationResultV1(_str(data["result"])),
        tested_item_ids=_str_tuple(data["tested_item_ids"]),
        evidence=_evidence_tuple(data["evidence"]),
        detail=_str(data["detail"]),
        evidence_level=(
            None
            if data.get("evidence_level") is None
            else ProtocolEvidenceLevelV1(_str(data["evidence_level"]))
        ),
    )


def _evidence_level(
    value: object,
    status: ProtocolValidationStatus,
    evidence: tuple[ProtocolEvidenceRefV1, ...] = (),
) -> ProtocolEvidenceLevelV1:
    return (
        _default_evidence_level(status, evidence)
        if value is None
        else ProtocolEvidenceLevelV1(_str(value))
    )


def _hypothesis_message_from_dict(value: object) -> HypothesisMessageV1:
    data = _strict_object(
        value,
        {
            "message_id",
            "name",
            "ordinal",
            "direction",
            "min_length",
            "max_length",
            "encrypted_opaque",
        },
    )
    return HypothesisMessageV1(
        message_id=_str(data["message_id"]),
        name=_str(data["name"]),
        ordinal=_int(data["ordinal"]),
        direction=StreamDirection(_str(data["direction"])),
        min_length=_int(data["min_length"]),
        max_length=_int(data["max_length"]),
        encrypted_opaque=_bool(data["encrypted_opaque"]),
    )


def _hypothesis_field_from_dict(value: object) -> HypothesisFieldV1:
    data = _strict_object(
        value,
        {
            "field_id",
            "message_id",
            "name",
            "offset",
            "length",
            "endianness",
            "encoding",
            "condition",
            "expected_hex",
            "encrypted_opaque",
            "justification",
        },
        optional_keys={"justification"},
    )
    return HypothesisFieldV1(
        field_id=_str(data["field_id"]),
        message_id=_str(data["message_id"]),
        name=_str(data["name"]),
        offset=_int(data["offset"]),
        length=_int(data["length"]),
        endianness=_optional_str(data["endianness"]),
        encoding=_optional_str(data["encoding"]),
        condition=_optional_str(data["condition"]),
        expected_hex=_optional_str(data["expected_hex"]),
        encrypted_opaque=_bool(data["encrypted_opaque"]),
        justification=_optional_str(data.get("justification")),
    )


def _hypothesis_characteristic_from_dict(value: object) -> HypothesisCharacteristicV1:
    data = _strict_object(value, {"kind", "name", "statement", "justification"})
    return HypothesisCharacteristicV1(
        kind=ProtocolCharacteristicKindV1(_str(data["kind"])),
        name=_str(data["name"]),
        statement=_str(data["statement"]),
        justification=_str(data["justification"]),
    )


def _hypothesis_transition_from_dict(value: object) -> HypothesisTransitionV1:
    data = _strict_object(
        value,
        {
            "transition_id",
            "from_state",
            "to_state",
            "message_id",
            "trigger",
        },
    )
    return HypothesisTransitionV1(
        transition_id=_str(data["transition_id"]),
        from_state=_str(data["from_state"]),
        to_state=_str(data["to_state"]),
        message_id=_str(data["message_id"]),
        trigger=_str(data["trigger"]),
    )

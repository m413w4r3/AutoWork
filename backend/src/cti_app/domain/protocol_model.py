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
    capture_sha256: str
    flow_id: str
    stream_id: str
    direction: StreamDirection
    frame_numbers: tuple[int, ...]
    start_offset: int | None
    end_offset: int | None
    source: str
    observation_id: UUID | None = None
    timestamp_start_ns: int | None = None
    timestamp_end_ns: int | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.capture_sha256):
            raise ValueError("evidence capture sha256 must be lowercase hexadecimal")
        if not self.flow_id or not self.stream_id or not self.source:
            raise ValueError("evidence flow, stream, and source are required")
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
    min_length: int
    max_length: int
    field_ids: tuple[str, ...]
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...]
    encrypted_opaque: bool = False

    def __post_init__(self) -> None:
        if not self.message_id or not self.name or self.ordinal < 1:
            raise ValueError("message identity and positive ordinal are required")
        if self.min_length < 0 or self.max_length < self.min_length:
            raise ValueError("message length range is invalid")
        if not self.evidence:
            raise ValueError("each message definition requires evidence")


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

    def __post_init__(self) -> None:
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

    def __post_init__(self) -> None:
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

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sample_sha256) or not _SHA256.fullmatch(self.binary_sha256):
            raise ValueError("binary provenance digests must be lowercase sha256")
        if not all((self.provider, self.provider_version, self.function_id)) or not self.evidence:
            raise ValueError("function provenance requires provider, function, and evidence")


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

    def __post_init__(self) -> None:
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
class ProtocolHypothesisProposalV1(_CanonicalContract):
    schema_version: str
    messages: tuple[HypothesisMessageV1, ...]
    fields: tuple[HypothesisFieldV1, ...]
    transitions: tuple[HypothesisTransitionV1, ...]
    test_plan: tuple[ProtocolTestPlanStepV1, ...]

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
            value, {"schema_version", "messages", "fields", "transitions", "test_plan"}
        )
        return cls(
            schema_version=_str(data["schema_version"]),
            messages=tuple(_hypothesis_message_from_dict(item) for item in _list(data["messages"])),
            fields=tuple(_hypothesis_field_from_dict(item) for item in _list(data["fields"])),
            transitions=tuple(
                _hypothesis_transition_from_dict(item) for item in _list(data["transitions"])
            ),
            test_plan=tuple(_test_step_from_dict(item) for item in _list(data["test_plan"])),
        )


@dataclass(frozen=True, slots=True)
class ProtocolDiagramNodeV1(_CanonicalContract):
    node_id: str
    label: str
    role: str
    validation_status: ProtocolValidationStatus
    evidence: tuple[ProtocolEvidenceRefV1, ...] = ()


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


def _strict_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("contract object has missing or unknown fields")
    return value


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
    data = _strict_object(
        value,
        {
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
        },
    )
    frames = _list(data["frame_numbers"])
    return ProtocolEvidenceRefV1(
        capture_sha256=_str(data["capture_sha256"]),
        flow_id=_str(data["flow_id"]),
        stream_id=_str(data["stream_id"]),
        direction=StreamDirection(_str(data["direction"])),
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
        },
    )
    return MessageDefinitionV1(
        message_id=_str(data["message_id"]),
        name=_str(data["name"]),
        ordinal=_int(data["ordinal"]),
        directions=tuple(StreamDirection(_str(item)) for item in _list(data["directions"])),
        min_length=_int(data["min_length"]),
        max_length=_int(data["max_length"]),
        field_ids=_str_tuple(data["field_ids"]),
        validation_status=ProtocolValidationStatus(_str(data["validation_status"])),
        evidence=_evidence_tuple(data["evidence"]),
        encrypted_opaque=_bool(data["encrypted_opaque"]),
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
        },
    )
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
        validation_status=ProtocolValidationStatus(_str(data["validation_status"])),
        stability=FieldStability(_str(data["stability"])),
        evidence=_evidence_tuple(data["evidence"]),
        value_sha256s=_str_tuple(data["value_sha256s"]),
        expected_hex=_optional_str(data["expected_hex"]),
        encrypted_opaque=_bool(data["encrypted_opaque"]),
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
        },
    )
    return ProtocolTransitionV1(
        transition_id=_str(data["transition_id"]),
        from_state=_str(data["from_state"]),
        to_state=_str(data["to_state"]),
        message_id=_str(data["message_id"]),
        trigger=_str(data["trigger"]),
        hypotheses=_str_tuple(data["hypotheses"]),
        validation_status=ProtocolValidationStatus(_str(data["validation_status"])),
        evidence=_evidence_tuple(data["evidence"]),
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
        },
    )
    return BinaryFunctionProvenanceV1(
        sample_sha256=_str(data["sample_sha256"]),
        binary_sha256=_str(data["binary_sha256"]),
        provider=_str(data["provider"]),
        provider_version=_str(data["provider_version"]),
        function_id=_str(data["function_id"]),
        evidence=_evidence_tuple(data["evidence"]),
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
        },
    )
    return ProtocolVerificationFindingV1(
        finding_id=_str(data["finding_id"]),
        item_id=_str(data["item_id"]),
        kind=_str(data["kind"]),
        validation_status=ProtocolValidationStatus(_str(data["validation_status"])),
        expected=_optional_str(data["expected"]),
        observed=_optional_str(data["observed"]),
        detail=_str(data["detail"]),
        evidence=_evidence_tuple(data["evidence"]),
    )


def _test_step_from_dict(value: object) -> ProtocolTestPlanStepV1:
    data = _strict_object(value, {"kind", "message_id", "field_id", "transition_id"})
    return ProtocolTestPlanStepV1(
        kind=ProtocolTestKind(_str(data["kind"])),
        message_id=_str(data["message_id"]),
        field_id=_optional_str(data["field_id"]),
        transition_id=_optional_str(data["transition_id"]),
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
        },
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

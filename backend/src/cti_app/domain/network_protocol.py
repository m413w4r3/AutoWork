"""Versioned, compact contracts for passive network capture analysis.

These contracts intentionally describe observations and bounded extraction
metadata. They do not model a decoded protocol; L6 can add that as a separate,
additive ``ProtocolModelV1`` contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CAPTURE_BYTES = 200 * 1024 * 1024
_MAX_PACKET_COUNT = 1_000_000
_MAX_STREAM_BYTES = 64 * 1024 * 1024


class NetworkCommandId(StrEnum):
    """Private, allowlisted command identifiers used by the L5 adapter."""

    TSHARK = "tshark"
    CAPINFOS = "capinfos"


class NetworkCaptureFormat(StrEnum):
    PCAP = "pcap"
    PCAPNG = "pcapng"


class NetworkCaptureRejection(StrEnum):
    INVALID_FORMAT = "invalid_format"
    SIZE_LIMIT = "size_limit"
    CORRUPT_CAPTURE = "corrupt_capture"
    PACKET_LIMIT = "packet_limit"
    BLOB_UNAVAILABLE = "blob_unavailable"
    BLOB_HASH_MISMATCH = "blob_hash_mismatch"
    CAPTURE_NOT_ASSOCIATED = "capture_not_associated"


class NetworkTransport(StrEnum):
    TCP = "tcp"
    UDP = "udp"
    OTHER = "other"
    UNKNOWN = "unknown"


class StreamDirection(StrEnum):
    ORIGINATOR_TO_RESPONDER = "originator_to_responder"
    RESPONDER_TO_ORIGINATOR = "responder_to_originator"
    UNRESOLVED = "unresolved"


class EvidenceStatus(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"


class FieldCandidateKind(StrEnum):
    FRAME_LENGTH = "frame_length"
    MAGIC = "magic"
    TYPE = "type"
    ID = "id"
    SESSION = "session"
    TIMESTAMP = "timestamp"
    COMPRESSION = "compression"
    CHECKSUM = "checksum"
    OPCODE = "opcode"
    PAYLOAD = "payload"


@dataclass(frozen=True, slots=True)
class NetworkExtractionLimitsV1:
    max_capture_bytes: int = 64 * 1024 * 1024
    max_packets: int = 100_000
    max_output_bytes: int = 2 * 1024 * 1024
    max_flows: int = 64
    max_endpoints: int = 128
    max_protocols: int = 64
    max_stream_bytes: int = 8 * 1024 * 1024
    max_messages: int = 64
    max_field_observations: int = 16
    max_field_value_chars: int = 128
    max_frame_refs_per_record: int = 16
    max_detail_ranges: int = 64
    max_preview_bytes: int = 32

    def __post_init__(self) -> None:
        values = (
            self.max_capture_bytes,
            self.max_packets,
            self.max_output_bytes,
            self.max_flows,
            self.max_endpoints,
            self.max_protocols,
            self.max_stream_bytes,
            self.max_messages,
            self.max_field_observations,
            self.max_field_value_chars,
            self.max_frame_refs_per_record,
            self.max_detail_ranges,
            self.max_preview_bytes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values
        ):
            raise ValueError("Network extraction limits must be positive integers")
        if self.max_capture_bytes > _MAX_CAPTURE_BYTES:
            raise ValueError(f"max_capture_bytes must be <= {_MAX_CAPTURE_BYTES}")
        if self.max_packets > _MAX_PACKET_COUNT:
            raise ValueError(f"max_packets must be <= {_MAX_PACKET_COUNT}")
        if self.max_stream_bytes > _MAX_STREAM_BYTES:
            raise ValueError(f"max_stream_bytes must be <= {_MAX_STREAM_BYTES}")


@dataclass(frozen=True, slots=True)
class ExtractorVersionV1:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class NetworkCaptureRefV1:
    blob_id: UUID
    sha256: str
    size_bytes: int
    format: NetworkCaptureFormat

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("capture sha256 must be lowercase hexadecimal")
        if not isinstance(self.blob_id, UUID):
            raise TypeError("capture blob_id must be a UUID")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes <= 0
        ):
            raise ValueError("capture size must be positive")
        if not isinstance(self.format, NetworkCaptureFormat):
            raise TypeError("capture format must use NetworkCaptureFormat")

    def to_dict(self) -> dict[str, Any]:
        return {
            "blob_id": str(self.blob_id),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "format": self.format.value,
        }


@dataclass(frozen=True, slots=True)
class NetworkEndpointV1:
    address: str
    port: int | None


@dataclass(frozen=True, slots=True)
class ProtocolCountV1:
    protocol: str
    packet_count: int


@dataclass(frozen=True, slots=True)
class NetworkFieldObservationV1:
    name: str
    value: str
    frame_number: int
    value_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ConversationSummaryV1:
    flow_id: str
    transport: NetworkTransport
    endpoint_a: NetworkEndpointV1
    endpoint_b: NetworkEndpointV1
    packet_count_a_to_b: int
    packet_count_b_to_a: int
    payload_bytes_a_to_b: int
    payload_bytes_b_to_a: int
    frame_numbers: tuple[int, ...]
    first_timestamp_ns: int | None
    last_timestamp_ns: int | None
    orientation_basis: str
    observed_intervals_ms: tuple[int, ...] = ()
    frame_numbers_truncated: bool = False


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
class NetworkCaptureReportV1(_CanonicalContract):
    schema_version: str
    capture: NetworkCaptureRefV1
    duration_ns: int | None
    packet_count: int
    truncated_packet_count: int
    unparsed_frame_count: int
    unparsed_frame_numbers: tuple[int, ...]
    unparsed_frames_truncated: bool
    protocols: tuple[ProtocolCountV1, ...]
    endpoints: tuple[NetworkEndpointV1, ...]
    conversations: tuple[ConversationSummaryV1, ...]
    dns_observations: tuple[NetworkFieldObservationV1, ...]
    http_observations: tuple[NetworkFieldObservationV1, ...]
    tls_observations: tuple[NetworkFieldObservationV1, ...]
    extractor_versions: tuple[ExtractorVersionV1, ...]
    limits: NetworkExtractionLimitsV1
    output_truncated: bool = False
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StreamGapV1:
    start_offset: int
    end_offset: int
    before_frame: int | None
    after_frame: int | None


@dataclass(frozen=True, slots=True)
class StreamOverlapV1:
    start_offset: int
    end_offset: int
    frame_numbers: tuple[int, ...]
    frame_numbers_truncated: bool
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class StreamSegmentV1:
    frame_number: int
    start_offset: int
    end_offset: int
    captured_bytes: int
    tcp_sequence: int | None
    frame_truncated: bool


@dataclass(frozen=True, slots=True)
class ReassembledStreamV1(_CanonicalContract):
    schema_version: str
    capture_sha256: str
    flow_id: str
    transport: NetworkTransport
    direction: StreamDirection
    direction_basis: str
    source: NetworkEndpointV1
    destination: NetworkEndpointV1
    frame_numbers: tuple[int, ...]
    frame_numbers_truncated: bool
    observed_payload_bytes: int
    stream_start_offset: int
    stream_end_offset: int
    segments: tuple[StreamSegmentV1, ...]
    segments_truncated: bool
    gaps: tuple[StreamGapV1, ...]
    details_truncated: bool
    retransmission_frames: tuple[int, ...]
    overlaps: tuple[StreamOverlapV1, ...]
    truncated_frames: tuple[int, ...]
    payload_truncated: bool
    encrypted_opaque: bool
    contiguous_payload_sha256: str | None
    masked_hex_preview: str
    extractor_versions: tuple[ExtractorVersionV1, ...]
    limits: NetworkExtractionLimitsV1
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MessageBoundaryV1:
    message_index: int
    start_offset: int
    end_offset: int
    frame_numbers: tuple[int, ...]
    support_messages: int
    status: EvidenceStatus
    frame_numbers_truncated: bool = False


@dataclass(frozen=True, slots=True)
class FieldCandidateV1:
    kind: FieldCandidateKind
    name: str
    offset: int | None
    length: int | None
    endianness: str | None
    status: EvidenceStatus
    support_messages: int
    frame_numbers: tuple[int, ...]
    frame_numbers_truncated: bool
    masked_examples: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class MessageSegmentationV1(_CanonicalContract):
    schema_version: str
    capture_sha256: str
    flow_id: str
    transport: NetworkTransport
    direction: StreamDirection
    boundaries: tuple[MessageBoundaryV1, ...]
    fields: tuple[FieldCandidateV1, ...]
    gaps: tuple[StreamGapV1, ...]
    details_truncated: bool
    encrypted_opaque: bool
    extractor_versions: tuple[ExtractorVersionV1, ...]
    limits: NetworkExtractionLimitsV1
    limitations: tuple[str, ...] = ()
    overlaps: tuple[StreamOverlapV1, ...] = ()


@dataclass(frozen=True, slots=True)
class ProtocolEvidenceFlowV1:
    flow_id: str
    transport: NetworkTransport
    frame_numbers: tuple[int, ...]
    frame_numbers_truncated: bool
    directions: tuple[str, ...]
    gaps: tuple[StreamGapV1, ...]
    overlaps: tuple[StreamOverlapV1, ...] = ()


@dataclass(frozen=True, slots=True)
class ProtocolEvidenceSequenceV1:
    flow_id: str
    direction: StreamDirection
    ordered_frames: tuple[int, ...]
    ordered_frames_truncated: bool
    message_boundaries: tuple[MessageBoundaryV1, ...]


@dataclass(frozen=True, slots=True)
class ProtocolEvidencePackV1(_CanonicalContract):
    schema_version: str
    capture_sha256: str
    flows: tuple[ProtocolEvidenceFlowV1, ...]
    field_candidates: tuple[FieldCandidateV1, ...]
    sequences: tuple[ProtocolEvidenceSequenceV1, ...]
    masked_hex_previews: tuple[str, ...]
    extractor_versions: tuple[ExtractorVersionV1, ...]
    limits: NetworkExtractionLimitsV1
    limitations: tuple[str, ...] = ()


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

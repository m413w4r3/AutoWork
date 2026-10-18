"""Passive PCAP acceptance, bounded inspection, stream recovery and segmentation."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from cti_app.application.blob_storage import BlobReadLimitExceededError, BlobStorageUnavailableError
from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.investigation import EvidenceKind, ToolInvocationStatus
from cti_app.domain.local_analysis import LocalCommandStatus
from cti_app.domain.network_protocol import (
    ConversationSummaryV1,
    EvidenceStatus,
    ExtractorVersionV1,
    FieldCandidateKind,
    FieldCandidateV1,
    MessageBoundaryV1,
    MessageSegmentationV1,
    NetworkCaptureFormat,
    NetworkCaptureRefV1,
    NetworkCaptureRejection,
    NetworkCaptureReportV1,
    NetworkEndpointV1,
    NetworkExtractionLimitsV1,
    NetworkFieldObservationV1,
    NetworkTransport,
    ProtocolCountV1,
    ProtocolEvidenceFlowV1,
    ProtocolEvidencePackV1,
    ProtocolEvidenceSequenceV1,
    ReassembledStreamV1,
    StreamDirection,
    StreamGapV1,
    StreamOverlapV1,
    StreamSegmentV1,
)
from cti_app.infrastructure.pcap_reader import (
    CapturedPacket,
    PcapReaderError,
    PcapReadResult,
    read_capture,
)
from cti_app.infrastructure.tshark_adapter import TsharkAdapter, TsharkInspection

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_LOCAL_CAPTURE_ASSOCIATIONS = 16_384
_NETWORK_CAPTURE_BUCKET = "network-captures-quarantine"
_CAPTURE_SOURCE_VERSION = "network-capture-acceptance.v1"
_PCAP_MIME = {
    NetworkCaptureFormat.PCAP: "application/vnd.tcpdump.pcap",
    NetworkCaptureFormat.PCAPNG: "application/x-pcapng",
}


class QuarantineCaptureTooLargeError(ValueError):
    """The quarantine adapter refused to materialize a capture over the limit."""


@dataclass(frozen=True, slots=True)
class QuarantinedCapture:
    source_reference: str
    content: bytes
    sha256: str


class QuarantinedCaptureSource(Protocol):
    async def read_quarantined_capture(
        self, source_reference: str, *, max_bytes: int
    ) -> QuarantinedCapture: ...


class NetworkCaptureBlobStore(Protocol):
    async def put_capture(self, content: bytes, *, mime_type: str) -> UUID: ...

    async def read_capture(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...


class ProductionNetworkCaptureBlobStore:
    """Use the canonical artifact/blob catalog for immutable capture bytes."""

    def __init__(self, artifacts: ProductionArtifactStore) -> None:
        self._artifacts = artifacts

    async def put_capture(self, content: bytes, *, mime_type: str) -> UUID:
        return await self._artifacts.put_bytes(
            content,
            bucket=_NETWORK_CAPTURE_BUCKET,
            mime_type=mime_type,
        )

    async def read_capture(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        return await self._artifacts.read_bytes(blob_id, max_bytes=max_bytes)


class NetworkCaptureRejected(ValueError):
    def __init__(self, reason: NetworkCaptureRejection) -> None:
        self.reason = reason
        super().__init__(reason.value)


class NetworkAnalysisError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class NetworkInspectionOutcome:
    status: ToolInvocationStatus
    report: NetworkCaptureReportV1 | None
    detail_code: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkStreamOutcome:
    status: ToolInvocationStatus
    streams: tuple[ReassembledStreamV1, ...]
    detail_code: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkSegmentationOutcome:
    status: ToolInvocationStatus
    segmentations: tuple[MessageSegmentationV1, ...]
    evidence_pack: ProtocolEvidencePackV1 | None
    detail_code: str | None = None


@dataclass(slots=True)
class _Flow:
    flow_id: str
    transport: NetworkTransport
    endpoint_a: NetworkEndpointV1
    endpoint_b: NetworkEndpointV1
    packets: list[CapturedPacket]


@dataclass(frozen=True, slots=True)
class _PayloadSegment:
    offset: int
    content: bytes
    packet: CapturedPacket


@dataclass(frozen=True, slots=True)
class _DatagramRange:
    start: int
    end: int
    frame_number: int


@dataclass(frozen=True, slots=True)
class _StreamMaterial:
    contract: ReassembledStreamV1
    data: bytes
    coverage: bytes
    datagrams: tuple[_DatagramRange, ...]
    payload_segments: tuple[_PayloadSegment, ...]


class NetworkWorkbench:
    def __init__(
        self,
        *,
        quarantine: QuarantinedCaptureSource,
        blobs: NetworkCaptureBlobStore,
        tshark: TsharkAdapter,
        recorder: EvidenceRecorder | None = None,
        limits: NetworkExtractionLimitsV1 | None = None,
    ) -> None:
        self.quarantine = quarantine
        self.blobs = blobs
        self.tshark = tshark
        self.recorder = recorder
        self.limits = limits or NetworkExtractionLimitsV1()
        self._accepted_capture_associations: set[tuple[UUID, UUID, str]] = set()

    async def accept_quarantined_capture(
        self,
        source_reference: str,
        *,
        investigation_id: UUID,
        idempotency_key: str | None = None,
    ) -> NetworkCaptureRefV1:
        if not source_reference.strip():
            raise NetworkCaptureRejected(NetworkCaptureRejection.INVALID_FORMAT)
        try:
            quarantined = await self.quarantine.read_quarantined_capture(
                source_reference,
                max_bytes=self.limits.max_capture_bytes,
            )
        except QuarantineCaptureTooLargeError as exc:
            raise NetworkCaptureRejected(NetworkCaptureRejection.SIZE_LIMIT) from exc
        except NetworkCaptureRejected:
            raise
        except (OSError, ValueError) as exc:
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_UNAVAILABLE) from exc
        if not isinstance(quarantined.content, bytes):
            raise NetworkCaptureRejected(NetworkCaptureRejection.CORRUPT_CAPTURE)
        if len(quarantined.content) > self.limits.max_capture_bytes:
            raise NetworkCaptureRejected(NetworkCaptureRejection.SIZE_LIMIT)
        actual_sha256 = hashlib.sha256(quarantined.content).hexdigest()
        if not _SHA256.fullmatch(quarantined.sha256) or quarantined.sha256 != actual_sha256:
            raise NetworkCaptureRejected(NetworkCaptureRejection.CORRUPT_CAPTURE)
        parsed = self._parse(quarantined.content)
        blob_id = await self.blobs.put_capture(
            quarantined.content,
            mime_type=_PCAP_MIME[parsed.format],
        )
        reference = NetworkCaptureRefV1(
            blob_id=blob_id,
            sha256=actual_sha256,
            size_bytes=len(quarantined.content),
            format=parsed.format,
        )
        if self.recorder is not None:
            await self.recorder.record_existing_blob(
                investigation_id=investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=blob_id,
                sha256=actual_sha256,
                source="network.quarantine",
                source_version=_CAPTURE_SOURCE_VERSION,
                object_type="network_capture",
                object_id=actual_sha256,
                limitations=(
                    "Le PCAP/PCAPNG complet est conservé dans le blob immuable de quarantaine.",
                    "Aucun trafic n'est capturé en direct et aucun binaire n'est exécuté.",
                ),
                idempotency_key=idempotency_key,
            )
        self._remember_capture_association(investigation_id, blob_id, actual_sha256)
        return reference

    async def inspect(
        self,
        reference: NetworkCaptureRefV1,
        *,
        investigation_id: UUID | None = None,
    ) -> NetworkInspectionOutcome:
        content, parsed = await self._load(reference, investigation_id=investigation_id)
        inspect_artifact = getattr(self.tshark, "inspect_artifact", None)
        if callable(inspect_artifact):
            assert investigation_id is not None
            result = await inspect_artifact(
                artifact_id=reference.blob_id,
                investigation_id=investigation_id,
                capture_sha256=reference.sha256,
            )
        else:
            result = await self.tshark.inspect(content, reference.sha256)
        if result.status is LocalCommandStatus.UNAVAILABLE:
            return NetworkInspectionOutcome(
                ToolInvocationStatus.UNAVAILABLE, None, result.detail_code or "unavailable"
            )
        if result.status not in {
            LocalCommandStatus.SUCCEEDED,
            LocalCommandStatus.OUTPUT_TRUNCATED,
            LocalCommandStatus.EMPTY,
        }:
            return NetworkInspectionOutcome(
                ToolInvocationStatus.FAILED, None, result.detail_code or "tshark_failed"
            )
        flows = _flows(parsed.packets)
        versions = _extractor_versions(result)
        report, truncated = self._capture_report(reference, parsed, flows, result, versions)
        status = ToolInvocationStatus.PARTIAL if truncated else ToolInvocationStatus.SUCCEEDED
        return NetworkInspectionOutcome(status, report)

    async def reassemble(
        self,
        reference: NetworkCaptureRefV1,
        flow_id: str,
        *,
        investigation_id: UUID | None = None,
    ) -> NetworkStreamOutcome:
        _, parsed = await self._load(reference, investigation_id=investigation_id)
        flow = _find_flow(parsed.packets, flow_id)
        if flow is None:
            return NetworkStreamOutcome(ToolInvocationStatus.EMPTY, (), "flow_not_found")
        materials = _reassemble_flow(flow, reference.sha256, self.limits)
        if not materials:
            return NetworkStreamOutcome(ToolInvocationStatus.EMPTY, (), "no_stream_data")
        streams = tuple(item.contract for item in materials)
        partial = any(
            item.contract.payload_truncated
            or item.contract.gaps
            or item.contract.details_truncated
            or any(overlap.ambiguous for overlap in item.contract.overlaps)
            or item.contract.truncated_frames
            for item in materials
        )
        return NetworkStreamOutcome(
            ToolInvocationStatus.PARTIAL if partial else ToolInvocationStatus.SUCCEEDED,
            streams,
        )

    async def segment(
        self,
        reference: NetworkCaptureRefV1,
        flow_id: str,
        *,
        investigation_id: UUID | None = None,
    ) -> NetworkSegmentationOutcome:
        _, parsed = await self._load(reference, investigation_id=investigation_id)
        flow = _find_flow(parsed.packets, flow_id)
        if flow is None:
            return NetworkSegmentationOutcome(
                ToolInvocationStatus.EMPTY, (), None, "flow_not_found"
            )
        materials = _reassemble_flow(flow, reference.sha256, self.limits)
        segmentations = tuple(_segment_material(item, self.limits) for item in materials)
        pack = _evidence_pack(reference.sha256, flow, materials, segmentations, self.limits)
        partial = any(
            material.contract.payload_truncated
            or material.contract.gaps
            or material.contract.details_truncated
            or any(overlap.ambiguous for overlap in material.contract.overlaps)
            or segmentation.details_truncated
            for material, segmentation in zip(materials, segmentations, strict=True)
        )
        return NetworkSegmentationOutcome(
            ToolInvocationStatus.PARTIAL if partial else ToolInvocationStatus.SUCCEEDED,
            segmentations,
            pack,
        )

    async def _load(
        self,
        reference: NetworkCaptureRefV1,
        *,
        investigation_id: UUID | None = None,
    ) -> tuple[bytes, PcapReadResult]:
        await self._require_capture_association(reference, investigation_id)
        if reference.size_bytes > self.limits.max_capture_bytes:
            raise NetworkCaptureRejected(NetworkCaptureRejection.SIZE_LIMIT)
        try:
            content = await self.blobs.read_capture(
                reference.blob_id,
                max_bytes=self.limits.max_capture_bytes,
            )
        except BlobReadLimitExceededError as exc:
            raise NetworkCaptureRejected(NetworkCaptureRejection.SIZE_LIMIT) from exc
        except (
            BlobStorageUnavailableError,
            ProductionReuseStorageUnavailableError,
            EntityNotFoundError,
            OSError,
            ValueError,
        ) as exc:
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_UNAVAILABLE) from exc
        if not isinstance(content, bytes):
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_UNAVAILABLE)
        if (
            len(content) != reference.size_bytes
            or hashlib.sha256(content).hexdigest() != reference.sha256
        ):
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_HASH_MISMATCH)
        parsed = self._parse(content)
        if parsed.format is not reference.format:
            raise NetworkCaptureRejected(NetworkCaptureRejection.INVALID_FORMAT)
        return content, parsed

    async def _require_capture_association(
        self,
        reference: NetworkCaptureRefV1,
        investigation_id: UUID | None,
    ) -> None:
        if investigation_id is None:
            raise NetworkCaptureRejected(NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED)
        association = (investigation_id, reference.blob_id, reference.sha256)
        if association in self._accepted_capture_associations:
            return
        checker = getattr(self.recorder, "is_capture_associated", None)
        if checker is not None and await checker(
            investigation_id=investigation_id,
            blob_id=reference.blob_id,
            sha256=reference.sha256,
        ):
            self._remember_capture_association(
                investigation_id, reference.blob_id, reference.sha256
            )
            return
        raise NetworkCaptureRejected(NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED)

    def _remember_capture_association(
        self, investigation_id: UUID, blob_id: UUID, sha256: str
    ) -> None:
        association = (investigation_id, blob_id, sha256)
        if association in self._accepted_capture_associations:
            return
        if len(self._accepted_capture_associations) >= _MAX_LOCAL_CAPTURE_ASSOCIATIONS:
            self._accepted_capture_associations.pop()
        self._accepted_capture_associations.add(association)

    def _parse(self, content: bytes) -> PcapReadResult:
        if not content or len(content) > self.limits.max_capture_bytes:
            reason = (
                NetworkCaptureRejection.SIZE_LIMIT
                if len(content) > self.limits.max_capture_bytes
                else NetworkCaptureRejection.INVALID_FORMAT
            )
            raise NetworkCaptureRejected(reason)
        try:
            return read_capture(content, max_packets=self.limits.max_packets)
        except PcapReaderError as exc:
            reason = (
                NetworkCaptureRejection.INVALID_FORMAT
                if exc.code in {"invalid_magic", "unsupported_link_type"}
                else NetworkCaptureRejection.PACKET_LIMIT
                if exc.code == "packet_limit"
                else NetworkCaptureRejection.CORRUPT_CAPTURE
            )
            raise NetworkCaptureRejected(reason) from exc

    def _capture_report(
        self,
        reference: NetworkCaptureRefV1,
        parsed: PcapReadResult,
        flows: Sequence[_Flow],
        inspection: TsharkInspection,
        versions: tuple[ExtractorVersionV1, ...],
    ) -> tuple[NetworkCaptureReportV1, bool]:
        packet_rows = inspection.packets
        protocol_counts: Counter[str] = Counter()
        ignored_protocol_layers = {"FRAME", "ETH", "ETHERTYPE", "DATA"}
        for row in packet_rows:
            if row.protocol.strip():
                protocol_counts[row.protocol.strip().upper()] += 1
            protocol_counts.update(
                layer.upper()
                for layer in row.protocol_stack.split(":")
                if layer and layer.upper() not in ignored_protocol_layers
            )
        protocols = tuple(
            ProtocolCountV1(protocol, count)
            for protocol, count in sorted(protocol_counts.items())[: self.limits.max_protocols]
        )
        output_truncated = len(protocol_counts) > self.limits.max_protocols
        endpoints_by_key: dict[tuple[str, int | None], NetworkEndpointV1] = {}
        for packet in parsed.packets:
            if packet.source_address is not None:
                endpoint = NetworkEndpointV1(packet.source_address, packet.source_port)
                endpoints_by_key[(endpoint.address, endpoint.port)] = endpoint
            if packet.destination_address is not None:
                endpoint = NetworkEndpointV1(packet.destination_address, packet.destination_port)
                endpoints_by_key[(endpoint.address, endpoint.port)] = endpoint
        endpoints = tuple(
            endpoints_by_key[key]
            for key in sorted(endpoints_by_key, key=lambda item: (item[0], item[1] or -1))[
                : self.limits.max_endpoints
            ]
        )
        output_truncated = output_truncated or len(endpoints_by_key) > self.limits.max_endpoints

        timestamps = [
            packet.timestamp_ns for packet in parsed.packets if packet.timestamp_ns is not None
        ]
        duration_ns = max(timestamps) - min(timestamps) if timestamps else None
        unparsed_frames = tuple(
            packet.frame_number for packet in parsed.packets if packet.transport is None
        )
        unparsed_frames_truncated = len(unparsed_frames) > self.limits.max_detail_ranges
        output_truncated = output_truncated or unparsed_frames_truncated
        summaries: list[ConversationSummaryV1] = []
        output_truncated = output_truncated or inspection.output_truncated
        for flow in flows[: self.limits.max_flows]:
            a_to_b = [packet for packet in flow.packets if _packet_direction(packet, flow) == 0]
            b_to_a = [packet for packet in flow.packets if _packet_direction(packet, flow) == 1]
            timestamps = sorted(
                packet.timestamp_ns for packet in flow.packets if packet.timestamp_ns is not None
            )
            intervals = tuple(
                max(0, (after - before) // 1_000_000) for before, after in pairwise(timestamps)
            )[: self.limits.max_frame_refs_per_record]
            output_truncated = output_truncated or max(0, len(timestamps) - 1) > len(intervals)
            frame_numbers = tuple(packet.frame_number for packet in flow.packets)
            frame_numbers_truncated = len(frame_numbers) > self.limits.max_frame_refs_per_record
            output_truncated = output_truncated or frame_numbers_truncated
            summaries.append(
                ConversationSummaryV1(
                    flow_id=flow.flow_id,
                    transport=flow.transport,
                    endpoint_a=flow.endpoint_a,
                    endpoint_b=flow.endpoint_b,
                    packet_count_a_to_b=len(a_to_b),
                    packet_count_b_to_a=len(b_to_a),
                    payload_bytes_a_to_b=sum(len(packet.payload) for packet in a_to_b),
                    payload_bytes_b_to_a=sum(len(packet.payload) for packet in b_to_a),
                    frame_numbers=frame_numbers[: self.limits.max_frame_refs_per_record],
                    first_timestamp_ns=min(timestamps) if timestamps else None,
                    last_timestamp_ns=max(timestamps) if timestamps else None,
                    orientation_basis="first_observed_packet",
                    observed_intervals_ms=intervals,
                    frame_numbers_truncated=frame_numbers_truncated,
                )
            )
        if len(flows) > self.limits.max_flows:
            output_truncated = True

        dns, http, tls = _field_observations(packet_rows, self.limits)
        if any(
            len(items) >= self.limits.max_field_observations
            or any(item.value_truncated for item in items)
            for items in (dns, http, tls)
        ):
            output_truncated = True
        truncated_packet_count = sum(packet.truncated for packet in parsed.packets)
        limitations = list(inspection.limitations)
        if unparsed_frames:
            limitations.append(
                "Certains frames ne fournissent pas de métadonnées TCP/UDP "
                "au parseur natif conservateur."
            )
        if output_truncated:
            limitations.append(
                "Les champs de synthèse sont bornés; le PCAP complet reste conservé."
            )
        report = NetworkCaptureReportV1(
            schema_version="NetworkCaptureReportV1",
            capture=reference,
            duration_ns=duration_ns,
            packet_count=len(parsed.packets),
            truncated_packet_count=truncated_packet_count,
            unparsed_frame_count=len(unparsed_frames),
            unparsed_frame_numbers=unparsed_frames[: self.limits.max_detail_ranges],
            unparsed_frames_truncated=unparsed_frames_truncated,
            protocols=protocols,
            endpoints=endpoints,
            conversations=tuple(summaries),
            dns_observations=dns,
            http_observations=http,
            tls_observations=tls,
            extractor_versions=versions,
            limits=self.limits,
            output_truncated=output_truncated,
            limitations=tuple(dict.fromkeys(limitations)),
        )
        return report, output_truncated


def _field_observations(
    rows: Sequence[object], limits: NetworkExtractionLimitsV1
) -> tuple[
    tuple[NetworkFieldObservationV1, ...],
    tuple[NetworkFieldObservationV1, ...],
    tuple[NetworkFieldObservationV1, ...],
]:
    from cti_app.infrastructure.tshark_adapter import TSHARK_FIELDS, TsharkPacketFields

    ja3_index = len(TSHARK_FIELDS) - 5
    dns: list[NetworkFieldObservationV1] = []
    http: list[NetworkFieldObservationV1] = []
    tls: list[NetworkFieldObservationV1] = []
    for raw_row in rows:
        if not isinstance(raw_row, TsharkPacketFields):
            continue
        fields = raw_row.values
        values = (
            (dns, "dns.query", 10),
            (http, "http.host", 12),
            (http, "http.method", 13),
            (http, "http.uri", 14),
            (tls, "tls.sni", 15),
        )
        for target, name, index in values:
            if index >= len(fields) or not fields[index]:
                continue
            value = fields[index]
            if name == "http.uri":
                try:
                    value = urlsplit(value).path
                except ValueError:
                    value = ""
            target.append(
                NetworkFieldObservationV1(
                    name,
                    value[: limits.max_field_value_chars],
                    raw_row.frame_number,
                    value_truncated=len(value) > limits.max_field_value_chars,
                )
            )
        if ja3_index < len(fields) and fields[ja3_index]:
            tls.append(
                NetworkFieldObservationV1(
                    "tls.ja3",
                    fields[ja3_index][: limits.max_field_value_chars],
                    raw_row.frame_number,
                    value_truncated=len(fields[ja3_index]) > limits.max_field_value_chars,
                )
            )
    return (
        tuple(dns[: limits.max_field_observations]),
        tuple(http[: limits.max_field_observations]),
        tuple(tls[: limits.max_field_observations]),
    )


def _flows(packets: Sequence[CapturedPacket]) -> list[_Flow]:
    by_key: dict[tuple[str, str, int, str, int], _Flow] = {}
    for packet in packets:
        if (
            packet.transport not in {NetworkTransport.TCP, NetworkTransport.UDP}
            or packet.source_address is None
            or packet.destination_address is None
            or packet.source_port is None
            or packet.destination_port is None
        ):
            continue
        source_key = (packet.source_address, packet.source_port)
        destination_key = (packet.destination_address, packet.destination_port)
        ordered = sorted((source_key, destination_key))
        key = (
            packet.transport.value,
            ordered[0][0],
            ordered[0][1],
            ordered[1][0],
            ordered[1][1],
        )
        flow = by_key.get(key)
        if flow is None:
            endpoint_a = NetworkEndpointV1(packet.source_address, packet.source_port)
            endpoint_b = NetworkEndpointV1(packet.destination_address, packet.destination_port)
            flow_id = hashlib.sha256(
                (
                    f"{packet.transport.value}|{ordered[0][0]}:{ordered[0][1]}|"
                    f"{ordered[1][0]}:{ordered[1][1]}"
                ).encode()
            ).hexdigest()[:24]
            flow = _Flow(flow_id, packet.transport, endpoint_a, endpoint_b, [])
            by_key[key] = flow
        flow.packets.append(packet)
    return sorted(by_key.values(), key=lambda item: item.flow_id)


def _find_flow(packets: Sequence[CapturedPacket], flow_id: str) -> _Flow | None:
    if not flow_id or len(flow_id) > 64:
        return None
    return next((flow for flow in _flows(packets) if flow.flow_id == flow_id), None)


def _packet_direction(packet: CapturedPacket, flow: _Flow) -> int:
    if (packet.source_address, packet.source_port) == (
        flow.endpoint_a.address,
        flow.endpoint_a.port,
    ):
        return 0
    return 1


def _reassemble_flow(
    flow: _Flow, capture_sha256: str, limits: NetworkExtractionLimitsV1
) -> tuple[_StreamMaterial, ...]:
    directions = (0, 1)
    materials: list[_StreamMaterial] = []
    for direction in directions:
        directional = [
            packet for packet in flow.packets if _packet_direction(packet, flow) == direction
        ]
        if not directional:
            continue
        source = flow.endpoint_a if direction == 0 else flow.endpoint_b
        destination = flow.endpoint_b if direction == 0 else flow.endpoint_a
        direction_name = (
            StreamDirection.ORIGINATOR_TO_RESPONDER
            if direction == 0
            else StreamDirection.RESPONDER_TO_ORIGINATOR
        )
        if flow.transport is NetworkTransport.UDP:
            materials.append(
                _reassemble_udp(
                    flow,
                    directional,
                    capture_sha256,
                    source,
                    destination,
                    direction_name,
                    limits,
                )
            )
        else:
            materials.append(
                _reassemble_tcp(
                    flow,
                    directional,
                    capture_sha256,
                    source,
                    destination,
                    direction_name,
                    limits,
                )
            )
    return tuple(materials)


def _reassemble_udp(
    flow: _Flow,
    packets: Sequence[CapturedPacket],
    capture_sha256: str,
    source: NetworkEndpointV1,
    destination: NetworkEndpointV1,
    direction: StreamDirection,
    limits: NetworkExtractionLimitsV1,
) -> _StreamMaterial:
    data = bytearray()
    datagrams: list[_DatagramRange] = []
    segments: list[_PayloadSegment] = []
    truncated_frames: list[int] = []
    for packet in packets:
        start = len(data)
        payload = packet.payload[: limits.max_stream_bytes - start]
        data.extend(payload)
        datagrams.append(_DatagramRange(start, len(data), packet.frame_number))
        if payload:
            segments.append(_PayloadSegment(start, payload, packet))
        if packet.truncated or len(payload) != len(packet.payload):
            truncated_frames.append(packet.frame_number)
        if len(data) >= limits.max_stream_bytes:
            break
    content = bytes(data)
    frame_numbers = tuple(
        packet.frame_number for packet in packets[: limits.max_frame_refs_per_record]
    )
    frame_numbers_truncated = len(packets) > len(frame_numbers)
    payload_truncated = bool(truncated_frames) or sum(
        len(packet.payload) for packet in packets
    ) > len(content)
    digest = hashlib.sha256(content).hexdigest() if not payload_truncated else None
    segments_truncated = len(segments) > limits.max_detail_ranges
    contract = ReassembledStreamV1(
        schema_version="ReassembledStreamV1",
        capture_sha256=capture_sha256,
        flow_id=flow.flow_id,
        transport=flow.transport,
        direction=direction,
        direction_basis="first_observed_packet",
        source=source,
        destination=destination,
        frame_numbers=frame_numbers,
        frame_numbers_truncated=frame_numbers_truncated,
        observed_payload_bytes=sum(len(packet.payload) for packet in packets),
        stream_start_offset=0,
        stream_end_offset=len(content),
        segments=tuple(
            StreamSegmentV1(
                frame_number=segment.packet.frame_number,
                start_offset=segment.offset,
                end_offset=segment.offset + len(segment.content),
                captured_bytes=len(segment.content),
                tcp_sequence=None,
                frame_truncated=segment.packet.truncated,
            )
            for segment in segments[: limits.max_detail_ranges]
        ),
        segments_truncated=segments_truncated,
        gaps=(),
        details_truncated=segments_truncated or len(truncated_frames) > limits.max_detail_ranges,
        retransmission_frames=(),
        overlaps=(),
        truncated_frames=tuple(truncated_frames[: limits.max_detail_ranges]),
        payload_truncated=payload_truncated,
        encrypted_opaque=False,
        contiguous_payload_sha256=digest,
        masked_hex_preview=_masked_hex(content, limits.max_preview_bytes),
        extractor_versions=(ExtractorVersionV1("native-pcap-reader", "1"),),
        limits=limits,
        limitations=(
            "UDP datagrams restent séparés comme frontières de transport observées.",
            (
                "L'orientation suit le premier paquet observé; les rôles client/serveur "
                "ne sont pas inférés."
            ),
        ),
    )
    return _StreamMaterial(
        contract, content, b"\x01" * len(content), tuple(datagrams), tuple(segments)
    )


def _reassemble_tcp(
    flow: _Flow,
    packets: Sequence[CapturedPacket],
    capture_sha256: str,
    source: NetworkEndpointV1,
    destination: NetworkEndpointV1,
    direction: StreamDirection,
    limits: NetworkExtractionLimitsV1,
) -> _StreamMaterial:
    payload_packets = [
        packet for packet in packets if packet.payload and packet.tcp_sequence is not None
    ]
    frame_numbers = tuple(
        packet.frame_number for packet in packets[: limits.max_frame_refs_per_record]
    )
    frame_numbers_truncated = len(packets) > len(frame_numbers)
    if not payload_packets:
        contract = ReassembledStreamV1(
            schema_version="ReassembledStreamV1",
            capture_sha256=capture_sha256,
            flow_id=flow.flow_id,
            transport=flow.transport,
            direction=direction,
            direction_basis="first_observed_packet",
            source=source,
            destination=destination,
            frame_numbers=frame_numbers,
            frame_numbers_truncated=frame_numbers_truncated,
            observed_payload_bytes=0,
            stream_start_offset=0,
            stream_end_offset=0,
            segments=(),
            segments_truncated=False,
            gaps=(),
            details_truncated=sum(packet.truncated for packet in packets)
            > limits.max_detail_ranges,
            retransmission_frames=(),
            overlaps=(),
            truncated_frames=tuple(packet.frame_number for packet in packets if packet.truncated)[
                : limits.max_detail_ranges
            ],
            payload_truncated=any(packet.truncated for packet in packets),
            encrypted_opaque=False,
            contiguous_payload_sha256=None,
            masked_hex_preview="",
            extractor_versions=(ExtractorVersionV1("native-pcap-reader", "1"),),
            limits=limits,
            limitations=(
                "Aucun octet de charge TCP n'est présent dans cette direction.",
                (
                    "L'orientation suit le premier paquet observé; les rôles client/serveur "
                    "ne sont pas inférés."
                ),
            ),
        )
        return _StreamMaterial(contract, b"", b"", (), ())

    first_sequence = payload_packets[0].tcp_sequence
    assert first_sequence is not None
    syn_packets = [
        packet for packet in packets if packet.tcp_flags & 0x02 and packet.tcp_sequence is not None
    ]
    syn_sequence = syn_packets[0].tcp_sequence if syn_packets else None
    expected_sequence = (
        ((syn_sequence + 1) & 0xFFFFFFFF) if syn_sequence is not None else first_sequence
    )
    segments_with_offset: list[tuple[int, CapturedPacket]] = []
    for packet in payload_packets:
        assert packet.tcp_sequence is not None
        delta = ((packet.tcp_sequence - expected_sequence + 0x80000000) & 0xFFFFFFFF) - 0x80000000
        segments_with_offset.append((delta, packet))
    if syn_sequence is None:
        origin = min(offset for offset, _ in segments_with_offset)
        segments_with_offset = [
            (offset - origin, packet) for offset, packet in segments_with_offset
        ]
    else:
        negative_offsets = [offset for offset, _ in segments_with_offset if offset < 0]
        if negative_offsets:
            shift = min(negative_offsets)
            segments_with_offset = [
                (offset - shift, packet) for offset, packet in segments_with_offset
            ]
    max_end = min(
        limits.max_stream_bytes,
        max(offset + len(packet.payload) for offset, packet in segments_with_offset),
    )
    data = bytearray(max_end)
    covered = bytearray(max_end)
    overlap_flags = bytearray(max_end)
    overlap_conflicts = bytearray(max_end)
    payload_segments: list[_PayloadSegment] = []
    retransmissions: list[int] = []
    truncated_frames = [packet.frame_number for packet in packets if packet.truncated]
    payload_truncated = bool(truncated_frames) or any(
        offset + len(packet.payload) > limits.max_stream_bytes
        for offset, packet in segments_with_offset
    )
    for offset, packet in segments_with_offset:
        available = max(0, min(len(packet.payload), max_end - offset))
        if available == 0:
            payload_truncated = True
            continue
        payload = packet.payload[:available]
        payload_segments.append(_PayloadSegment(offset, payload, packet))
        fully_seen = True
        conflict = False
        for index, byte_value in enumerate(payload, start=offset):
            if covered[index]:
                overlap_flags[index] = 1
                if data[index] != byte_value:
                    overlap_conflicts[index] = 1
                    conflict = True
                continue
            fully_seen = False
            data[index] = byte_value
            covered[index] = 1
        if fully_seen and not conflict:
            retransmissions.append(packet.frame_number)

    all_gaps = _ranges_from_coverage(covered, payload_segments)
    all_overlaps = _ranges_from_flags(
        overlap_flags,
        overlap_conflicts,
        payload_segments,
        max_frame_refs=limits.max_frame_refs_per_record,
    )
    details_truncated = (
        len(all_gaps) > limits.max_detail_ranges
        or len(all_overlaps) > limits.max_detail_ranges
        or any(item.frame_numbers_truncated for item in all_overlaps)
        or len(payload_segments) > limits.max_detail_ranges
        or len(retransmissions) > limits.max_detail_ranges
        or len(truncated_frames) > limits.max_detail_ranges
    )
    gaps = all_gaps[: limits.max_detail_ranges]
    overlap_intervals = all_overlaps[: limits.max_detail_ranges]
    content = bytes(data)
    complete = not gaps and not payload_truncated and not any(overlap_conflicts)
    tls_opaque = _looks_like_encrypted_tls(content, covered)
    contract = ReassembledStreamV1(
        schema_version="ReassembledStreamV1",
        capture_sha256=capture_sha256,
        flow_id=flow.flow_id,
        transport=flow.transport,
        direction=direction,
        direction_basis="first_observed_packet",
        source=source,
        destination=destination,
        frame_numbers=frame_numbers,
        frame_numbers_truncated=frame_numbers_truncated,
        observed_payload_bytes=sum(len(packet.payload) for packet in payload_packets),
        stream_start_offset=0,
        stream_end_offset=max_end,
        segments=tuple(
            StreamSegmentV1(
                frame_number=segment.packet.frame_number,
                start_offset=segment.offset,
                end_offset=segment.offset + len(segment.content),
                captured_bytes=len(segment.content),
                tcp_sequence=segment.packet.tcp_sequence,
                frame_truncated=segment.packet.truncated,
            )
            for segment in payload_segments[: limits.max_detail_ranges]
        ),
        segments_truncated=len(payload_segments) > limits.max_detail_ranges,
        gaps=gaps,
        details_truncated=details_truncated,
        retransmission_frames=tuple(retransmissions[: limits.max_detail_ranges]),
        overlaps=overlap_intervals,
        truncated_frames=tuple(truncated_frames[: limits.max_detail_ranges]),
        payload_truncated=payload_truncated,
        encrypted_opaque=tls_opaque,
        contiguous_payload_sha256=hashlib.sha256(content).hexdigest() if complete else None,
        masked_hex_preview=_masked_hex(content, limits.max_preview_bytes),
        extractor_versions=(ExtractorVersionV1("native-pcap-reader", "1"),),
        limits=limits,
        limitations=(
            (
                "TLS applicatif détecté, charge conservée opaque; "
                "aucun secret de déchiffrement n'est utilisé."
            ),
            (
                "L'orientation suit le premier paquet observé; "
                "les rôles client/serveur ne sont pas inférés."
            ),
        )
        if tls_opaque
        else (
            "L'orientation suit le premier paquet observé; "
            "les rôles client/serveur ne sont pas inférés.",
        ),
    )
    return _StreamMaterial(contract, content, bytes(covered), (), tuple(payload_segments))


def _ranges_from_coverage(
    covered: bytearray, segments: Sequence[_PayloadSegment]
) -> tuple[StreamGapV1, ...]:
    ranges: list[StreamGapV1] = []
    cursor = 0
    while cursor < len(covered):
        if covered[cursor]:
            cursor += 1
            continue
        start = cursor
        while cursor < len(covered) and not covered[cursor]:
            cursor += 1
        before = [segment for segment in segments if segment.offset + len(segment.content) <= start]
        after = [segment for segment in segments if segment.offset >= cursor]
        before_frame = (
            max(before, key=lambda item: item.offset + len(item.content)).packet.frame_number
            if before
            else None
        )
        after_frame = (
            min(after, key=lambda item: item.offset).packet.frame_number if after else None
        )
        ranges.append(StreamGapV1(start, cursor, before_frame, after_frame))
    return tuple(ranges)


def _ranges_from_flags(
    overlap_flags: bytearray,
    conflict_flags: bytearray,
    segments: Sequence[_PayloadSegment],
    *,
    max_frame_refs: int,
) -> tuple[StreamOverlapV1, ...]:
    ranges: list[StreamOverlapV1] = []
    cursor = 0
    while cursor < len(overlap_flags):
        if not overlap_flags[cursor]:
            cursor += 1
            continue
        start = cursor
        conflict = False
        while cursor < len(overlap_flags) and overlap_flags[cursor]:
            conflict = conflict or bool(conflict_flags[cursor])
            cursor += 1
        all_frames = tuple(
            sorted(
                {
                    segment.packet.frame_number
                    for segment in segments
                    if segment.offset < cursor and segment.offset + len(segment.content) > start
                }
            )
        )
        ranges.append(
            StreamOverlapV1(
                start,
                cursor,
                all_frames[:max_frame_refs],
                len(all_frames) > max_frame_refs,
                conflict,
            )
        )
    return tuple(ranges)


def _looks_like_encrypted_tls(data: bytes, coverage: bytearray) -> bool:
    for offset in range(max(0, len(data) - 4)):
        if (
            all(coverage[offset + relative] for relative in range(5))
            and data[offset] == 0x17
            and data[offset + 1] == 0x03
            and data[offset + 2] in range(0, 5)
        ):
            length = int.from_bytes(data[offset + 3 : offset + 5], "big")
            if 0 < length <= 18_432:
                return True
    return False


def _masked_hex(data: bytes, max_bytes: int) -> str:
    preview = data[:max_bytes]
    if len(preview) <= 8:
        return " ".join(f"{value:02x}" for value in preview)
    parts = [
        f"{value:02x}" if index < 4 or index >= len(preview) - 2 else "**"
        for index, value in enumerate(preview)
    ]
    return " ".join(parts)


def _segment_material(
    material: _StreamMaterial, limits: NetworkExtractionLimitsV1
) -> MessageSegmentationV1:
    contract = material.contract
    boundaries: list[MessageBoundaryV1] = []
    fields: list[FieldCandidateV1] = []
    if contract.transport is NetworkTransport.UDP:
        for index, datagram in enumerate(material.datagrams[: limits.max_messages], start=1):
            frame_numbers = (datagram.frame_number,)
            boundaries.append(
                MessageBoundaryV1(
                    message_index=index,
                    start_offset=datagram.start,
                    end_offset=datagram.end,
                    frame_numbers=frame_numbers,
                    support_messages=len(material.datagrams),
                    status=EvidenceStatus.OBSERVED,
                )
            )
        if boundaries:
            frames = tuple(sorted({frame for item in boundaries for frame in item.frame_numbers}))
            fields.append(
                FieldCandidateV1(
                    FieldCandidateKind.PAYLOAD,
                    "udp_datagram_payload",
                    0,
                    None,
                    None,
                    EvidenceStatus.OBSERVED,
                    len(boundaries),
                    frames[: limits.max_frame_refs_per_record],
                    len(frames) > limits.max_frame_refs_per_record,
                    (_masked_hex(material.data, limits.max_preview_bytes),),
                    "Chaque datagramme UDP fournit une frontière de transport observée.",
                )
            )
    else:
        candidates = _length_prefix_candidates(material, limits)
        selected = candidates[0] if len(candidates) == 1 else None
        if len(candidates) > 1:
            candidate_frames = tuple(
                sorted(
                    {
                        frame
                        for candidate in candidates
                        for start, end in candidate[3]
                        for frame in _frames_for_range(material.payload_segments, start, end)
                    }
                )
            )
            for width, endian, length_includes_prefix, parsed in candidates[
                : limits.max_field_observations
            ]:
                fields.append(
                    FieldCandidateV1(
                        FieldCandidateKind.FRAME_LENGTH,
                        f"length_prefix_candidate_{width}_{endian}_{length_includes_prefix}",
                        parsed[0][0],
                        width,
                        "big" if endian == ">" else "little",
                        EvidenceStatus.UNRESOLVED,
                        len(parsed),
                        candidate_frames[: limits.max_frame_refs_per_record],
                        len(candidate_frames) > limits.max_frame_refs_per_record,
                        tuple(
                            _masked_hex(material.data[start : start + width], width)
                            for start, _ in parsed[:4]
                        ),
                        "Plusieurs interprétations complètes du préfixe restent compatibles.",
                    )
                )
        if selected is not None:
            width, endian, length_includes_prefix, parsed = selected
            for index, (start, end) in enumerate(parsed[: limits.max_messages], start=1):
                frames = _frames_for_range(material.payload_segments, start, end)
                boundaries.append(
                    MessageBoundaryV1(
                        message_index=index,
                        start_offset=start,
                        end_offset=end,
                        frame_numbers=frames[: limits.max_frame_refs_per_record],
                        support_messages=len(parsed),
                        status=EvidenceStatus.INFERRED,
                        frame_numbers_truncated=len(frames) > limits.max_frame_refs_per_record,
                    )
                )
            evidence_frames = tuple(
                sorted(
                    {
                        frame
                        for start, end in parsed
                        for frame in _frames_for_range(material.payload_segments, start, end)
                    }
                )
            )
            examples = tuple(
                _masked_hex(material.data[start : start + width], width) for start, _ in parsed[:4]
            )
            fields.append(
                FieldCandidateV1(
                    FieldCandidateKind.FRAME_LENGTH,
                    "length_prefix",
                    parsed[0][0],
                    width,
                    "big" if endian == ">" else "little",
                    EvidenceStatus.INFERRED,
                    len(parsed),
                    evidence_frames[: limits.max_frame_refs_per_record],
                    len(evidence_frames) > limits.max_frame_refs_per_record,
                    examples,
                    "Le préfixe permet de parcourir plusieurs messages complets; "
                    f"longueur totale inclut le préfixe: {length_includes_prefix}.",
                )
            )
            payload_offset = parsed[0][0] + width
            magic = _common_prefix(material.data, parsed, width, max_bytes=8)
            if len(magic) >= 2:
                fields.append(
                    FieldCandidateV1(
                        FieldCandidateKind.MAGIC,
                        "repeated_message_prefix",
                        payload_offset,
                        len(magic),
                        None,
                        EvidenceStatus.INFERRED,
                        len(parsed),
                        evidence_frames[: limits.max_frame_refs_per_record],
                        len(evidence_frames) > limits.max_frame_refs_per_record,
                        (_masked_hex(magic, len(magic)),),
                        "Même séquence d'octets observée au début de plusieurs messages.",
                    )
                )
            body_examples = tuple(
                _masked_hex(material.data[start + width : min(end, start + width + 8)], 8)
                for start, end in parsed[:4]
            )
            fields.append(
                FieldCandidateV1(
                    FieldCandidateKind.PAYLOAD,
                    "message_body",
                    payload_offset,
                    None,
                    None,
                    EvidenceStatus.OBSERVED,
                    len(parsed),
                    evidence_frames[: limits.max_frame_refs_per_record],
                    len(evidence_frames) > limits.max_frame_refs_per_record,
                    body_examples,
                    "Octets observés après le préfixe; "
                    "aucune sémantique applicative n'est affirmée.",
                )
            )
            opcode_values = [
                material.data[start + width] for start, end in parsed if start + width < end
            ]
            if len(opcode_values) == len(parsed) and len(set(opcode_values)) > 1:
                opcode_frames = tuple(
                    sorted(
                        {
                            frame
                            for start, end in parsed
                            for frame in _frames_for_range(
                                material.payload_segments, start + width, start + width + 1
                            )
                        }
                    )
                )
                fields.append(
                    FieldCandidateV1(
                        FieldCandidateKind.OPCODE,
                        "first_body_byte",
                        payload_offset,
                        1,
                        None,
                        EvidenceStatus.INFERRED,
                        len(opcode_values),
                        opcode_frames[: limits.max_frame_refs_per_record],
                        len(opcode_frames) > limits.max_frame_refs_per_record,
                        tuple(f"{value:02x}" for value in opcode_values[:4]),
                        "Valeur d'octet distincte à position constante; rôle opcode à valider.",
                    )
                )
        elif material.data:
            payload_name = (
                "encrypted_opaque_payload"
                if contract.encrypted_opaque
                else "unframed_observed_bytes"
            )
            fields.append(
                FieldCandidateV1(
                    FieldCandidateKind.PAYLOAD,
                    payload_name,
                    0,
                    None,
                    None,
                    EvidenceStatus.UNRESOLVED,
                    0,
                    contract.frame_numbers[: limits.max_frame_refs_per_record],
                    contract.frame_numbers_truncated,
                    (_masked_hex(material.data, limits.max_preview_bytes),),
                    (
                        "Charge TLS chiffrée opaque; aucune frontière applicative n'est inférée."
                        if contract.encrypted_opaque
                        else "Aucune frontière de message n'est mesurée avec assez de support."
                    ),
                )
            )
        evidence_frames = contract.frame_numbers
        known_kinds = {item.kind for item in fields}
        unresolved_fields = (
            (FieldCandidateKind.FRAME_LENGTH, "frame_length"),
            (FieldCandidateKind.MAGIC, "magic_signature"),
            (FieldCandidateKind.TYPE, "message_type"),
            (FieldCandidateKind.OPCODE, "opcode"),
            (FieldCandidateKind.ID, "message_id"),
            (FieldCandidateKind.SESSION, "session_id"),
            (FieldCandidateKind.TIMESTAMP, "timestamp"),
            (FieldCandidateKind.COMPRESSION, "compression_marker"),
            (FieldCandidateKind.CHECKSUM, "checksum"),
        )
        for kind, name in unresolved_fields:
            if kind in known_kinds:
                continue
            fields.append(
                FieldCandidateV1(
                    kind,
                    name,
                    None,
                    None,
                    None,
                    EvidenceStatus.UNRESOLVED,
                    0,
                    evidence_frames[: limits.max_frame_refs_per_record],
                    contract.frame_numbers_truncated,
                    (),
                    "Aucune règle déterministe suffisante ne soutient ce champ "
                    "dans les octets observés.",
                )
            )
    limitations = list(contract.limitations)
    if contract.gaps:
        limitations.append(
            "La segmentation s'arrête aux lacunes; aucune frontière ne les traverse."
        )
    if contract.encrypted_opaque:
        limitations.append("La charge TLS chiffrée est opaque et n'a pas été décodée.")
    if any(overlap.ambiguous for overlap in contract.overlaps):
        limitations.append(
            "Les octets en chevauchement ambigu sont exclus de l'inférence des frontières."
        )
    details_truncated = (
        contract.details_truncated
        or len(boundaries) >= limits.max_messages
        or any(item.frame_numbers_truncated for item in boundaries)
        or any(item.frame_numbers_truncated for item in fields)
    )
    if details_truncated:
        limitations.append("Les références détaillées sont bornées par les limites explicites.")
    return MessageSegmentationV1(
        schema_version="MessageSegmentationV1",
        capture_sha256=contract.capture_sha256,
        flow_id=contract.flow_id,
        transport=contract.transport,
        direction=contract.direction,
        boundaries=tuple(boundaries),
        fields=tuple(fields),
        gaps=contract.gaps,
        details_truncated=details_truncated,
        encrypted_opaque=contract.encrypted_opaque,
        extractor_versions=(ExtractorVersionV1("native-message-segmenter", "1"),),
        limits=limits,
        limitations=tuple(dict.fromkeys(limitations)),
        overlaps=contract.overlaps,
    )


def _length_prefix_candidates(
    material: _StreamMaterial, limits: NetworkExtractionLimitsV1
) -> list[tuple[int, str, bool, tuple[tuple[int, int], ...]]]:
    if material.contract.encrypted_opaque:
        return []
    coverage = bytearray(material.coverage)
    for overlap in material.contract.overlaps:
        if overlap.ambiguous:
            coverage[overlap.start_offset : overlap.end_offset] = b"\x00" * (
                overlap.end_offset - overlap.start_offset
            )
    candidates: list[tuple[int, str, bool, tuple[tuple[int, int], ...]]] = []
    for run_start, run_end in _covered_runs(coverage):
        if run_end - run_start < 4:
            continue
        for width in (1, 2, 4):
            for endian in (">", "<"):
                for includes_prefix in (False, True):
                    parsed: list[tuple[int, int]] = []
                    cursor = run_start
                    valid = True
                    while cursor < run_end and len(parsed) <= limits.max_messages:
                        if cursor + width > run_end:
                            valid = False
                            break
                        length = int.from_bytes(
                            material.data[cursor : cursor + width],
                            "big" if endian == ">" else "little",
                        )
                        total_length = length if includes_prefix else width + length
                        if total_length <= width or cursor + total_length > run_end:
                            valid = False
                            break
                        parsed.append((cursor, cursor + total_length))
                        cursor += total_length
                    if valid and cursor == run_end and len(parsed) >= 2:
                        candidates.append((width, endian, includes_prefix, tuple(parsed)))
    candidates.sort(key=lambda item: (-len(item[3]), item[0], item[1], item[2]))
    if not candidates:
        return []
    best_support = len(candidates[0][3])
    best = [candidate for candidate in candidates if len(candidate[3]) == best_support]
    # The caller emits boundaries only when exactly one best interpretation remains.
    return best


def _covered_runs(coverage: bytes | bytearray) -> tuple[tuple[int, int], ...]:
    runs: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(coverage):
        if not coverage[cursor]:
            cursor += 1
            continue
        start = cursor
        while cursor < len(coverage) and coverage[cursor]:
            cursor += 1
        runs.append((start, cursor))
    return tuple(runs)


def _frames_for_range(segments: Sequence[_PayloadSegment], start: int, end: int) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                segment.packet.frame_number
                for segment in segments
                if segment.offset < end and segment.offset + len(segment.content) > start
            }
        )
    )


def _common_prefix(
    data: bytes,
    parsed: Sequence[tuple[int, int]],
    prefix_width: int,
    *,
    max_bytes: int,
) -> bytes:
    examples = [
        data[start + prefix_width : min(end, start + prefix_width + max_bytes)]
        for start, end in parsed
    ]
    if not examples:
        return b""
    length = min(len(item) for item in examples)
    cursor = 0
    while cursor < length and all(item[cursor] == examples[0][cursor] for item in examples[1:]):
        cursor += 1
    return examples[0][:cursor]


def _evidence_pack(
    capture_sha256: str,
    flow: _Flow,
    materials: Sequence[_StreamMaterial],
    segmentations: Sequence[MessageSegmentationV1],
    limits: NetworkExtractionLimitsV1,
) -> ProtocolEvidencePackV1:
    fields = tuple(field for segmentation in segmentations for field in segmentation.fields)
    gaps = tuple(gap for item in materials for gap in item.contract.gaps)
    overlaps = tuple(overlap for item in materials for overlap in item.contract.overlaps)
    direction_names = tuple(item.contract.direction.value for item in materials)
    flow_record = ProtocolEvidenceFlowV1(
        flow_id=flow.flow_id,
        transport=flow.transport,
        frame_numbers=tuple(
            packet.frame_number for packet in flow.packets[: limits.max_frame_refs_per_record]
        ),
        frame_numbers_truncated=len(flow.packets) > limits.max_frame_refs_per_record,
        directions=direction_names,
        gaps=gaps,
        overlaps=overlaps,
    )
    sequences = tuple(
        ProtocolEvidenceSequenceV1(
            flow_id=flow.flow_id,
            direction=segmentation.direction,
            ordered_frames=material.contract.frame_numbers,
            ordered_frames_truncated=material.contract.frame_numbers_truncated,
            message_boundaries=segmentation.boundaries,
        )
        for material, segmentation in zip(materials, segmentations, strict=True)
    )
    versions = (
        ExtractorVersionV1("native-pcap-reader", "1"),
        ExtractorVersionV1("native-message-segmenter", "1"),
    )
    limitations = tuple(
        dict.fromkeys(
            limitation for segmentation in segmentations for limitation in segmentation.limitations
        )
    )
    return ProtocolEvidencePackV1(
        schema_version="ProtocolEvidencePackV1",
        capture_sha256=capture_sha256,
        flows=(flow_record,),
        field_candidates=fields,
        sequences=sequences,
        masked_hex_previews=tuple(
            item.contract.masked_hex_preview
            for item in materials
            if item.contract.masked_hex_preview
        )[:4],
        extractor_versions=versions,
        limits=limits,
        limitations=limitations,
    )


def _extractor_versions(inspection: TsharkInspection) -> tuple[ExtractorVersionV1, ...]:
    return (
        ExtractorVersionV1("native-pcap-reader", "1"),
        ExtractorVersionV1("tshark", inspection.tshark_version or "unavailable"),
        ExtractorVersionV1("capinfos", inspection.capinfos_version or "unavailable"),
    )

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import shutil
import struct
from collections.abc import Callable
from uuid import UUID, uuid5

import pytest

from cti_app.application.investigation_network_tools import (
    NETWORK_CAPTURE_INSPECT,
    NETWORK_MESSAGES_SEGMENT,
    NETWORK_STREAM_REASSEMBLE,
    network_tool_capabilities,
    register_network_tools,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolExecutionContext,
)
from cti_app.application.local_analysis import LocalCommandCatalog
from cti_app.application.network_workbench import (
    NetworkCaptureRejected,
    NetworkWorkbench,
    QuarantineCaptureTooLargeError,
    QuarantinedCapture,
    _field_observations,
    _flows,
)
from cti_app.domain.investigation import InvestigationBudget, ToolInvocationStatus
from cti_app.domain.local_analysis import LocalCommandStatus
from cti_app.domain.network_protocol import (
    EvidenceStatus,
    FieldCandidateKind,
    NetworkCaptureFormat,
    NetworkCaptureRefV1,
    NetworkCaptureRejection,
    NetworkExtractionLimitsV1,
)
from cti_app.infrastructure import local_command_runner as local_runner_module
from cti_app.infrastructure.local_command_runner import LocalCommandRunner
from cti_app.infrastructure.pcap_reader import PcapReaderError, read_capture
from cti_app.infrastructure.tshark_adapter import (
    TSHARK_FIELDS,
    TsharkAdapter,
    TsharkInspection,
    TsharkPacketFields,
    network_local_command_specs,
)

_BLOB_NAMESPACE = UUID("ed0b9892-9b57-4f73-850a-0867e2230c60")
_NETWORK_INVESTIGATION_ID = UUID("03e8a72e-8d8c-4b39-8c12-79db601dca42")
_OTHER_INVESTIGATION_ID = UUID("37ef2c3a-9c05-4dbc-ad93-860e24d2c492")


class _Quarantine:
    def __init__(self, content: bytes, *, claimed_sha256: str | None = None) -> None:
        self.content = content
        self.claimed_sha256 = claimed_sha256 or hashlib.sha256(content).hexdigest()

    async def read_quarantined_capture(
        self, source_reference: str, *, max_bytes: int
    ) -> QuarantinedCapture:
        if len(self.content) > max_bytes:
            raise QuarantineCaptureTooLargeError
        return QuarantinedCapture(source_reference, self.content, self.claimed_sha256)


class _CaptureBlobs:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}
        self.read_calls = 0

    async def put_capture(self, content: bytes, *, mime_type: str) -> UUID:
        blob_id = uuid5(_BLOB_NAMESPACE, hashlib.sha256(content).hexdigest())
        self.contents[blob_id] = content
        return blob_id

    async def read_capture(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        self.read_calls += 1
        data = self.contents[blob_id]
        if len(data) > max_bytes:
            raise ValueError("too large")
        return data


class _TestConfinedLocalCommandRunner(LocalCommandRunner):
    """Explicit local test adapter for running installed tools without host bwrap."""

    @property
    def filesystem_confined(self) -> bool:
        return True


def _tcp_frame(
    *,
    source: str = "10.0.0.2",
    destination: str = "203.0.113.10",
    source_port: int = 41000,
    destination_port: int = 443,
    sequence: int,
    acknowledgement: int = 0,
    flags: int = 0x10,
    payload: bytes = b"",
) -> bytes:
    tcp_header = struct.pack(
        "!HHIIHHHH",
        source_port,
        destination_port,
        sequence,
        acknowledgement,
        (5 << 12) | flags,
        65_535,
        0,
        0,
    )
    ip_length = 20 + len(tcp_header) + len(payload)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        ip_length,
        1,
        0,
        64,
        6,
        0,
        ipaddress.IPv4Address(source).packed,
        ipaddress.IPv4Address(destination).packed,
    )
    ethernet = bytes.fromhex("00112233445566778899aabb0800")
    return ethernet + ip_header + tcp_header + payload


def _udp_frame(*, source_port: int, destination_port: int, payload: bytes) -> bytes:
    udp_header = struct.pack("!HHHH", source_port, destination_port, 8 + len(payload), 0)
    ip_length = 20 + len(udp_header) + len(payload)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        ip_length,
        2,
        0,
        64,
        17,
        0,
        ipaddress.IPv4Address("10.0.0.2").packed,
        ipaddress.IPv4Address("8.8.8.8").packed,
    )
    ethernet = bytes.fromhex("00112233445566778899aabb0800")
    return ethernet + ip_header + udp_header + payload


def _pcap(
    frames: tuple[bytes, ...],
    *,
    timestamps: tuple[tuple[int, int], ...] | None = None,
    original_lengths: tuple[int, ...] | None = None,
    link_type: int = 1,
) -> bytes:
    data = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, link_type))
    times = timestamps or tuple(
        (1 + index * 100_000 // 1_000_000, index * 100_000 % 1_000_000)
        for index in range(len(frames))
    )
    originals = original_lengths or tuple(map(len, frames))
    for frame, (seconds, microseconds), original_length in zip(
        frames, times, originals, strict=True
    ):
        data.extend(struct.pack("<IIII", seconds, microseconds, len(frame), original_length))
        data.extend(frame)
    return bytes(data)


def _handshake_payload_capture(*, duplicate: bool = True) -> bytes:
    message_one = b"\x00\x03\x01AB"
    message_two = b"\x00\x03\x02CD"
    frames = [
        _tcp_frame(sequence=100, flags=0x02),
        _tcp_frame(
            source="203.0.113.10",
            destination="10.0.0.2",
            source_port=443,
            destination_port=41000,
            sequence=500,
            acknowledgement=101,
            flags=0x12,
        ),
        _tcp_frame(sequence=101, acknowledgement=501, flags=0x10),
        _tcp_frame(sequence=101, acknowledgement=501, flags=0x18, payload=message_one),
        _tcp_frame(sequence=106, acknowledgement=501, flags=0x18, payload=message_two),
    ]
    if duplicate:
        frames.append(
            _tcp_frame(sequence=106, acknowledgement=501, flags=0x18, payload=message_two)
        )
    timestamps = (*((1, index * 100_000) for index in range(3)), (2, 0), (3, 0))
    if duplicate:
        timestamps = (*timestamps, (4, 0))
    return _pcap(tuple(frames), timestamps=timestamps)


def _mixed_capture() -> bytes:
    http = b"GET /health HTTP/1.1\r\nHost: beacon.test\r\n\r\n"
    dns = (
        struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        + b"\x04test\x07example\x00"
        + struct.pack("!HH", 1, 1)
    )
    tls = b"\x17\x03\x03\x00\x04\xde\xad\xbe\xef"
    frames = (
        _tcp_frame(sequence=100, flags=0x02),
        _tcp_frame(sequence=101, flags=0x18, payload=tls),
        _udp_frame(source_port=53000, destination_port=53, payload=dns),
        _tcp_frame(
            source_port=42000,
            destination_port=80,
            sequence=1,
            flags=0x18,
            payload=http,
        ),
    )
    return _pcap(frames)


def _gap_capture() -> bytes:
    frames = (
        _tcp_frame(sequence=100, flags=0x02),
        _tcp_frame(sequence=101, flags=0x18, payload=b"abc"),
        _tcp_frame(sequence=106, flags=0x18, payload=b"fgh"),
    )
    return _pcap(frames)


def _ambiguous_overlap_capture() -> bytes:
    frames = (
        _tcp_frame(sequence=100, flags=0x02),
        _tcp_frame(sequence=101, flags=0x18, payload=b"abcdef"),
        _tcp_frame(sequence=104, flags=0x18, payload=b"ZZ"),
    )
    return _pcap(frames)


def _ambiguous_message_overlap_capture() -> bytes:
    messages = b"\x00\x03\x01AB\x00\x03\x02CD"
    frames = (
        _tcp_frame(sequence=100, flags=0x02),
        _tcp_frame(sequence=101, flags=0x18, payload=messages),
        _tcp_frame(sequence=108, flags=0x18, payload=b"\x09"),
    )
    return _pcap(frames)


def _pcapng_one_frame(frame: bytes, *, link_type: int = 1) -> bytes:
    section = struct.pack("<IIIHHqI", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1, 28)
    interface = struct.pack("<IIHHII", 1, 20, link_type, 0, 65_535, 20)
    padded = frame + b"\x00" * ((4 - len(frame) % 4) % 4)
    block_length = 32 + len(padded)
    packet = (
        struct.pack(
            "<IIIIIII",
            6,
            block_length,
            0,
            0,
            1_000_000,
            len(frame),
            len(frame),
        )
        + padded
        + struct.pack("<I", block_length)
    )
    return section + interface + packet


def _unavailable_tshark(limits: NetworkExtractionLimitsV1 | None = None) -> TsharkAdapter:
    return TsharkAdapter(
        catalog=LocalCommandCatalog(
            specs=network_local_command_specs(limits),
            which=lambda _: None,
        ),
        runner=LocalCommandRunner(require_filesystem_confinement=False),
        limits=limits,
    )


def _test_tshark_adapter(
    limits: NetworkExtractionLimitsV1 | None,
    monkeypatch: pytest.MonkeyPatch,
) -> TsharkAdapter:
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    runner = _TestConfinedLocalCommandRunner(
        network_sandbox_probe_seconds=0.01,
        require_filesystem_confinement=False,
    )
    catalog = LocalCommandCatalog(specs=network_local_command_specs(limits))
    return TsharkAdapter(runner=runner, catalog=catalog, limits=limits)


def _workbench(
    content: bytes,
    *,
    tshark: TsharkAdapter | None = None,
    limits: NetworkExtractionLimitsV1 | None = None,
) -> tuple[NetworkWorkbench, _CaptureBlobs]:
    blobs = _CaptureBlobs()
    return (
        NetworkWorkbench(
            quarantine=_Quarantine(content),
            blobs=blobs,
            tshark=tshark or _unavailable_tshark(limits),
            limits=limits,
        ),
        blobs,
    )


async def _accepted_reference(
    workbench: NetworkWorkbench,
    investigation_id: UUID = _NETWORK_INVESTIGATION_ID,
) -> NetworkCaptureRefV1:
    return await workbench.accept_quarantined_capture(
        "quarantine:pcap:one",
        investigation_id=investigation_id,
    )


def test_native_pcap_reader_reports_packet_metadata_and_pcapng() -> None:
    frame = _tcp_frame(sequence=10, payload=b"abc")
    classic = read_capture(_pcap((frame,)))
    assert classic.format is NetworkCaptureFormat.PCAP
    assert classic.packets[0].transport.value == "tcp"
    assert classic.packets[0].payload == b"abc"

    next_generation = read_capture(_pcapng_one_frame(frame))
    assert next_generation.format is NetworkCaptureFormat.PCAPNG
    assert next_generation.packets[0].payload == b"abc"


@pytest.mark.asyncio
async def test_zero_packet_capture_is_success_without_claiming_no_network() -> None:
    class _EmptyTshark:
        async def inspect(self, *_args, **_kwargs) -> TsharkInspection:
            return TsharkInspection(
                status=LocalCommandStatus.EMPTY,
                detail_code=None,
                tshark_version="4.0.17",
                capinfos_version="4.0.17",
                packets=(),
                capinfos_summary="0 packets",
                output_truncated=False,
                ja3_available=False,
            )

    workbench, _ = _workbench(_pcap(()), tshark=_EmptyTshark())  # type: ignore[arg-type]
    reference = await _accepted_reference(workbench)

    outcome = await workbench.inspect(reference, investigation_id=_NETWORK_INVESTIGATION_ID)

    assert outcome.status is ToolInvocationStatus.SUCCEEDED
    assert outcome.report is not None
    assert outcome.report.packet_count == 0
    assert outcome.report.conversations == ()
    assert any(
        "Aucun paquet capturé" in limitation and "absence de trafic réseau" in limitation
        for limitation in outcome.report.limitations
    )


@pytest.mark.parametrize(
    ("link_type", "encapsulate", "expected_format"),
    (
        (101, lambda frame: frame[14:], NetworkCaptureFormat.PCAP),
        (
            113,
            lambda frame: struct.pack("!HHH8sH", 0, 1, 6, b"\x00" * 8, 0x0800) + frame[14:],
            NetworkCaptureFormat.PCAP,
        ),
        (
            276,
            lambda frame: struct.pack("!HHIHBB8s", 0x0800, 0, 1, 1, 0, 6, b"\x00" * 8) + frame[14:],
            NetworkCaptureFormat.PCAPNG,
        ),
    ),
)
def test_pcap_reader_extracts_raw_and_linux_cooked_tcp(
    link_type: int,
    encapsulate: Callable[[bytes], bytes],
    expected_format: NetworkCaptureFormat,
) -> None:
    frame = _tcp_frame(sequence=10, payload=b"cooked")
    cooked_frame = encapsulate(frame)
    capture = (
        _pcapng_one_frame(cooked_frame, link_type=link_type)
        if expected_format is NetworkCaptureFormat.PCAPNG
        else _pcap((cooked_frame,), link_type=link_type)
    )

    parsed = read_capture(capture)

    assert parsed.format is expected_format
    assert parsed.packets[0].link_type == link_type
    assert parsed.packets[0].transport.value == "tcp"
    assert parsed.packets[0].payload == b"cooked"


@pytest.mark.asyncio
async def test_acceptance_hashes_and_stores_only_valid_quarantined_capture() -> None:
    capture = _handshake_payload_capture()
    workbench, blobs = _workbench(capture)
    reference = await _accepted_reference(workbench)
    assert reference.sha256 == hashlib.sha256(capture).hexdigest()
    assert reference.size_bytes == len(capture)
    assert reference.format is NetworkCaptureFormat.PCAP
    assert blobs.contents[reference.blob_id] == capture


@pytest.mark.asyncio
async def test_acceptance_rejects_format_size_corruption_and_source_hash_mismatch() -> None:
    for content, limits, claimed_hash, expected in (
        (b"not a capture", None, None, NetworkCaptureRejection.INVALID_FORMAT),
        (
            _handshake_payload_capture(),
            NetworkExtractionLimitsV1(max_capture_bytes=16),
            None,
            NetworkCaptureRejection.SIZE_LIMIT,
        ),
        (_pcap((b"x",))[:-1], None, None, NetworkCaptureRejection.CORRUPT_CAPTURE),
        (_handshake_payload_capture(), None, "0" * 64, NetworkCaptureRejection.CORRUPT_CAPTURE),
    ):
        workbench, _ = _workbench(content, limits=limits)
        workbench.quarantine = _Quarantine(content, claimed_sha256=claimed_hash)
        with pytest.raises(NetworkCaptureRejected) as raised:
            await _accepted_reference(workbench)
        assert raised.value.reason is expected


@pytest.mark.parametrize(
    ("max_capture_bytes", "max_packets", "max_stream_bytes"),
    (
        (200 * 1024 * 1024 + 1, 100_000, 8 * 1024 * 1024),
        (64 * 1024 * 1024, 1_000_001, 8 * 1024 * 1024),
        (64 * 1024 * 1024, 100_000, 64 * 1024 * 1024 + 1),
    ),
)
def test_network_limits_enforce_hard_capture_packet_and_stream_ceilings(
    max_capture_bytes: int,
    max_packets: int,
    max_stream_bytes: int,
) -> None:
    with pytest.raises(ValueError):
        NetworkExtractionLimitsV1(
            max_capture_bytes=max_capture_bytes,
            max_packets=max_packets,
            max_stream_bytes=max_stream_bytes,
        )


@pytest.mark.asyncio
async def test_capture_reference_cannot_cross_investigations_before_blob_or_tshark_read() -> None:
    class _TsharkSpy:
        available = True

        def __init__(self) -> None:
            self.calls = 0

        async def inspect(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("unauthorized captures must not reach TShark")

    capture = _mixed_capture()
    blobs = _CaptureBlobs()
    tshark = _TsharkSpy()
    workbench = NetworkWorkbench(
        quarantine=_Quarantine(capture),
        blobs=blobs,
        tshark=tshark,  # type: ignore[arg-type]
    )
    reference = await _accepted_reference(workbench, investigation_id=_NETWORK_INVESTIGATION_ID)

    with pytest.raises(NetworkCaptureRejected) as raised:
        await workbench.inspect(reference, investigation_id=_OTHER_INVESTIGATION_ID)

    assert raised.value.reason is NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED
    assert blobs.read_calls == 0
    assert tshark.calls == 0

    class _Recorder:
        async def record_json(self, **_kwargs):
            raise AssertionError("rejected captures must not create an observation")

    registry = InMemoryToolRegistry()
    register_network_tools(registry, workbench=workbench, recorder=_Recorder())  # type: ignore[arg-type]
    context = ToolExecutionContext(
        investigation_id=_OTHER_INVESTIGATION_ID,
        envelope=None,
        budget=InvestigationBudget(),
        idempotency_key="network-foreign-capture",
    )
    _definition, adapter = registry.get(NETWORK_CAPTURE_INSPECT, "1")  # type: ignore[misc]
    result = await adapter.execute({"capture_ref": reference.to_dict()}, context)
    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output["detail_code"] == "capture_not_associated"
    assert blobs.read_calls == 0
    assert tshark.calls == 0


@pytest.mark.asyncio
async def test_native_reassembly_marks_retransmission_gaps_and_encrypted_tls_opaque() -> None:
    capture = _handshake_payload_capture()
    workbench, _ = _workbench(capture)
    reference = await _accepted_reference(workbench)
    parsed = read_capture(capture)
    from cti_app.application.network_workbench import _flows

    flow = _flows(parsed.packets)[0]
    outcome = await workbench.reassemble(
        reference, flow.flow_id, investigation_id=_NETWORK_INVESTIGATION_ID
    )
    stream = outcome.streams[0]
    assert stream.retransmission_frames == (6,)
    assert stream.gaps == ()
    assert [
        (item.frame_number, item.start_offset, item.end_offset) for item in stream.segments
    ] == [
        (4, 0, 5),
        (5, 5, 10),
        (6, 5, 10),
    ]
    assert outcome.status is ToolInvocationStatus.SUCCEEDED

    segmentation = await workbench.segment(
        reference, flow.flow_id, investigation_id=_NETWORK_INVESTIGATION_ID
    )
    one_direction = segmentation.segmentations[0]
    assert len(one_direction.boundaries) == 2
    assert all(item.status is EvidenceStatus.INFERRED for item in one_direction.boundaries)
    assert any(item.kind.value == "frame_length" for item in one_direction.fields)
    assert any(
        item.kind is FieldCandidateKind.TYPE and item.status is EvidenceStatus.UNRESOLVED
        for item in one_direction.fields
    )
    assert segmentation.evidence_pack is not None
    assert segmentation.evidence_pack.schema_version == "ProtocolEvidencePackV1"

    periodic_flow = _flows(parsed.packets)[0]
    periodic_intervals_ms = tuple(
        (after.timestamp_ns - before.timestamp_ns) // 1_000_000
        for before, after in zip(periodic_flow.packets, periodic_flow.packets[1:], strict=False)
        if before.timestamp_ns is not None and after.timestamp_ns is not None
    )
    assert 1_000 in periodic_intervals_ms

    gap_capture = _gap_capture()
    gap_workbench, _ = _workbench(gap_capture)
    gap_reference = await _accepted_reference(gap_workbench)
    gap_flow = _flows(read_capture(gap_capture).packets)[0]
    gap_stream = (
        await gap_workbench.reassemble(
            gap_reference, gap_flow.flow_id, investigation_id=_NETWORK_INVESTIGATION_ID
        )
    ).streams[0]
    assert [(item.start_offset, item.end_offset) for item in gap_stream.gaps] == [(3, 5)]
    assert gap_stream.contiguous_payload_sha256 is None

    overlap_capture = _ambiguous_overlap_capture()
    overlap_workbench, _ = _workbench(overlap_capture)
    overlap_reference = await _accepted_reference(overlap_workbench)
    overlap_flow = _flows(read_capture(overlap_capture).packets)[0]
    overlap_outcome = await overlap_workbench.reassemble(
        overlap_reference,
        overlap_flow.flow_id,
        investigation_id=_NETWORK_INVESTIGATION_ID,
    )
    assert overlap_outcome.status is ToolInvocationStatus.PARTIAL
    assert overlap_outcome.streams[0].overlaps[0].ambiguous is True
    assert overlap_outcome.streams[0].contiguous_payload_sha256 is None

    tls_capture = _mixed_capture()
    tls_workbench, _ = _workbench(tls_capture)
    tls_reference = await _accepted_reference(tls_workbench)
    tls_flow = next(
        flow for flow in _flows(read_capture(tls_capture).packets) if flow.endpoint_a.port == 41000
    )
    tls_stream = (
        await tls_workbench.reassemble(
            tls_reference, tls_flow.flow_id, investigation_id=_NETWORK_INVESTIGATION_ID
        )
    ).streams[0]
    assert tls_stream.encrypted_opaque is True
    assert "opaque" in tls_stream.limitations[0]

    truncated_frame = _tcp_frame(sequence=101, payload=b"partial")
    truncated_capture = _pcap(
        (truncated_frame,),
        original_lengths=(len(truncated_frame) + 4,),
    )
    truncated_workbench, _ = _workbench(truncated_capture)
    truncated_reference = await _accepted_reference(truncated_workbench)
    truncated_flow = _flows(read_capture(truncated_capture).packets)[0]
    truncated_stream = (
        await truncated_workbench.reassemble(
            truncated_reference,
            truncated_flow.flow_id,
            investigation_id=_NETWORK_INVESTIGATION_ID,
        )
    ).streams[0]
    assert truncated_stream.payload_truncated is True
    assert truncated_stream.truncated_frames == (1,)
    assert truncated_stream.contiguous_payload_sha256 is None


@pytest.mark.asyncio
async def test_segment_preserves_overlaps_and_does_not_infer_conflicted_or_tls_messages() -> None:
    overlap_capture = _ambiguous_message_overlap_capture()
    overlap_workbench, _ = _workbench(overlap_capture)
    overlap_reference = await _accepted_reference(overlap_workbench)
    overlap_flow = _flows(read_capture(overlap_capture).packets)[0]
    overlap_outcome = await overlap_workbench.segment(
        overlap_reference,
        overlap_flow.flow_id,
        investigation_id=_NETWORK_INVESTIGATION_ID,
    )

    assert overlap_outcome.status is ToolInvocationStatus.PARTIAL
    assert overlap_outcome.evidence_pack is not None
    assert any(
        overlap.ambiguous
        for segmentation in overlap_outcome.segmentations
        for overlap in segmentation.overlaps
    )
    assert any(
        overlap.ambiguous
        for flow in overlap_outcome.evidence_pack.flows
        for overlap in flow.overlaps
    )
    assert not any(segmentation.boundaries for segmentation in overlap_outcome.segmentations)

    tls_capture = _mixed_capture()
    tls_workbench, _ = _workbench(tls_capture)
    tls_reference = await _accepted_reference(tls_workbench)
    tls_flow = next(
        flow
        for flow in _flows(read_capture(tls_capture).packets)
        if flow.endpoint_a.port == 41000 or flow.endpoint_b.port == 41000
    )
    tls_outcome = await tls_workbench.segment(
        tls_reference,
        tls_flow.flow_id,
        investigation_id=_NETWORK_INVESTIGATION_ID,
    )
    opaque = next(item for item in tls_outcome.segmentations if item.encrypted_opaque)
    assert opaque.boundaries == ()
    assert not any(item.status is EvidenceStatus.INFERRED for item in opaque.fields)


@pytest.mark.asyncio
async def test_unavailable_tshark_is_reported_without_suppressing_native_tools() -> None:
    capture = _mixed_capture()
    tshark = _unavailable_tshark()
    workbench, _ = _workbench(capture, tshark=tshark)
    reference = await _accepted_reference(workbench)
    outcome = await workbench.inspect(reference, investigation_id=_NETWORK_INVESTIGATION_ID)
    assert outcome.status is ToolInvocationStatus.UNAVAILABLE
    assert outcome.detail_code == "binary_unavailable"
    assert NETWORK_CAPTURE_INSPECT not in network_tool_capabilities(tshark)
    assert NETWORK_STREAM_REASSEMBLE in network_tool_capabilities(tshark)
    assert NETWORK_MESSAGES_SEGMENT in network_tool_capabilities(tshark)


@pytest.mark.asyncio
async def test_tshark_requires_filesystem_confinement_and_reports_typed_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    runner = LocalCommandRunner(require_filesystem_confinement=False)
    catalog = LocalCommandCatalog(
        specs=network_local_command_specs(),
        which=lambda _name: "/test-only/tshark",
    )
    tshark = TsharkAdapter(runner=runner, catalog=catalog)

    assert all(spec.requires_filesystem_confinement for spec in network_local_command_specs())
    assert not tshark.available
    assert NETWORK_CAPTURE_INSPECT not in network_tool_capabilities(tshark)
    outcome = await tshark.inspect(_mixed_capture(), hashlib.sha256(_mixed_capture()).hexdigest())
    assert outcome.status.value == "unavailable"
    assert outcome.detail_code == "filesystem_confinement_required"


def test_http_uri_observations_strip_credentials_query_and_fragment() -> None:
    values = [""] * (len(TSHARK_FIELDS) - 5)
    values[14] = "http://user:password@example.test/private?token=secret#fragment"
    row = TsharkPacketFields(1, None, 64, "HTTP", "frame:eth:ip:tcp:http", tuple(values))

    _dns, http, _tls = _field_observations((row,), NetworkExtractionLimitsV1())

    uri = next(item.value for item in http if item.name == "http.uri")
    assert uri == "/private"
    assert not any(secret in uri for secret in ("user", "password", "token", "secret", "fragment"))


@pytest.mark.asyncio
async def test_tools_register_with_versioned_bounded_schemas() -> None:
    workbench, _ = _workbench(_mixed_capture())

    class _Recorder:
        async def record_json(self, **kwargs):
            raise AssertionError("unavailable inspect must not record a report")

    registry = InMemoryToolRegistry()
    register_network_tools(registry, workbench=workbench, recorder=_Recorder())  # type: ignore[arg-type]
    context = ToolExecutionContext(
        investigation_id=_NETWORK_INVESTIGATION_ID,
        envelope=None,
        budget=InvestigationBudget(),
        idempotency_key="network-missing-tshark",
    )
    definition, adapter = registry.get(NETWORK_CAPTURE_INSPECT, "1")  # type: ignore[misc]
    result = await adapter.execute(
        {
            "capture_ref": (
                await _accepted_reference(workbench, investigation_id=context.investigation_id)
            ).to_dict()
        },
        context,
    )
    assert definition.capability_key == NETWORK_CAPTURE_INSPECT
    assert definition.cost_model
    assert result.status is ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_tshark_capinfos_inspection_and_deterministic_report_when_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await asyncio.wait_for(_exercise_tshark_capinfos_report(monkeypatch), timeout=180)


async def _exercise_tshark_capinfos_report(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _mixed_capture()
    if not shutil.which("tshark") or not shutil.which("capinfos"):
        tshark = _unavailable_tshark()
        workbench, _ = _workbench(capture, tshark=tshark)
        reference = await _accepted_reference(workbench)
        result = await workbench.inspect(reference, investigation_id=_NETWORK_INVESTIGATION_ID)
        assert result.status is ToolInvocationStatus.UNAVAILABLE
        assert result.detail_code == "binary_unavailable"
        return

    tshark = _test_tshark_adapter(None, monkeypatch)
    assert tshark.available
    workbench, _ = _workbench(capture, tshark=tshark)
    reference = await _accepted_reference(workbench)
    first = await asyncio.wait_for(
        workbench.inspect(reference, investigation_id=_NETWORK_INVESTIGATION_ID), timeout=80
    )
    second = await asyncio.wait_for(
        workbench.inspect(reference, investigation_id=_NETWORK_INVESTIGATION_ID), timeout=80
    )
    assert first.report is not None
    assert second.report is not None
    assert first.report.capture.sha256 == hashlib.sha256(capture).hexdigest()
    assert first.report.packet_count == 4
    assert first.report.duration_ns is not None
    assert first.report.conversations
    assert any(item.protocol == "DNS" for item in first.report.protocols)
    assert any(item.protocol == "HTTP" for item in first.report.protocols)
    assert any(
        item.name == "dns.query" and item.value == "test.example"
        for item in first.report.dns_observations
    )
    assert any(
        item.name == "http.host" and item.value == "beacon.test"
        for item in first.report.http_observations
    )
    assert any(
        item.name == "http.uri" and item.value == "/health"
        for item in first.report.http_observations
    )
    assert any(item.protocol.startswith("TLS") for item in first.report.protocols)
    assert first.report.canonical_json_bytes() == second.report.canonical_json_bytes()
    assert first.report.canonical_sha256 == second.report.canonical_sha256

    beacon_capture = _handshake_payload_capture()
    beacon_workbench, _ = _workbench(beacon_capture, tshark=tshark)
    beacon_reference = await _accepted_reference(beacon_workbench)
    _beacon_bytes, beacon_parsed = await beacon_workbench._load(
        beacon_reference, investigation_id=_NETWORK_INVESTIGATION_ID
    )
    beacon_inspection = TsharkInspection(
        status=LocalCommandStatus.SUCCEEDED,
        detail_code=None,
        tshark_version="test-runner",
        capinfos_version="test-runner",
        packets=(),
        capinfos_summary="",
        output_truncated=False,
        ja3_available=False,
    )
    beacon_report, _beacon_truncated = beacon_workbench._capture_report(
        beacon_reference,
        beacon_parsed,
        _flows(beacon_parsed.packets),
        beacon_inspection,
        (),
    )
    assert any(
        1_000 in conversation.observed_intervals_ms for conversation in beacon_report.conversations
    )


@pytest.mark.asyncio
async def test_tshark_output_limit_is_visible_and_report_stays_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not shutil.which("tshark") or not shutil.which("capinfos"):
        pytest.skip("TShark/capinfos capability is not installed")
    frames = tuple(_tcp_frame(sequence=100 + index, payload=b"x") for index in range(100))
    capture = _pcap(frames)
    limits = NetworkExtractionLimitsV1(max_output_bytes=512)
    tshark = _test_tshark_adapter(limits, monkeypatch)
    assert tshark.available
    workbench, _ = _workbench(capture, tshark=tshark, limits=limits)
    reference = await _accepted_reference(workbench)
    outcome = await asyncio.wait_for(
        workbench.inspect(reference, investigation_id=_NETWORK_INVESTIGATION_ID), timeout=80
    )
    assert outcome.report is not None
    assert outcome.status is ToolInvocationStatus.PARTIAL
    assert outcome.report.output_truncated is True
    assert len(outcome.report.canonical_json_bytes()) < 128 * 1024


def test_truncated_and_corrupt_pcap_have_typed_reader_errors() -> None:
    valid_header = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1)
    truncated_packet = valid_header + struct.pack("<IIII", 1, 0, 12, 12) + b"\x00\x01"
    with pytest.raises(PcapReaderError) as raised:
        read_capture(truncated_packet)
    assert raised.value.code == "corrupt_capture"

    invalid = b"\x00\x00\x00\x00"
    with pytest.raises(PcapReaderError) as raised:
        read_capture(invalid)
    assert raised.value.code == "invalid_magic"

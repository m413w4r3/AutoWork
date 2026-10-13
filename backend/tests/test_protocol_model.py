from __future__ import annotations

import hashlib
import ipaddress
import struct
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.application.investigation_protocol_tools import (
    NETWORK_PROTOCOL_COMPARE,
    NETWORK_PROTOCOL_HYPOTHESIZE,
    NETWORK_PROTOCOL_RENDER,
    NETWORK_PROTOCOL_VERIFY,
    PROTOCOL_TOOL_NAMES,
    protocol_tool_capabilities,
    register_protocol_tools,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry
from cti_app.application.network_protocol_model import (
    HypothesisNotReplayableError,
    NetworkProtocolWorkbench,
    ProtocolTraceRequestV1,
)
from cti_app.application.network_workbench import (
    NetworkWorkbench,
    QuarantinedCapture,
    _flows,
)
from cti_app.domain.network_protocol import (
    NetworkExtractionLimitsV1,
    StreamDirection,
)
from cti_app.domain.protocol_model import (
    AssuranceLevel,
    HypothesisFieldV1,
    HypothesisMessageV1,
    HypothesisTransitionV1,
    ProtocolDiagramKind,
    ProtocolDiagramStatus,
    ProtocolHypothesisProposalV1,
    ProtocolModelV1,
    ProtocolTestKind,
    ProtocolTestPlanStepV1,
    ProtocolValidationStatus,
)
from cti_app.infrastructure.pcap_reader import read_capture

_INVESTIGATION_ID = UUID("2c2ae64e-a882-4cc0-b0cf-3bff6d111ba2")
_BLOB_NAMESPACE = UUID("ed0b9892-9b57-4f73-850a-0867e2230c60")


class _Quarantine:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def read_quarantined_capture(self, source_reference: str, *, max_bytes: int):
        if len(self.content) > max_bytes:
            raise ValueError("capture too large")
        return QuarantinedCapture(
            source_reference,
            self.content,
            hashlib.sha256(self.content).hexdigest(),
        )


class _CaptureBlobs:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}

    async def put_capture(self, content: bytes, *, mime_type: str) -> UUID:
        blob_id = uuid5(_BLOB_NAMESPACE, hashlib.sha256(content).hexdigest())
        self.contents[blob_id] = content
        return blob_id

    async def read_capture(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self.contents[blob_id]
        if len(content) > max_bytes:
            raise ValueError("capture too large")
        return content


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


def _pcap(frames: tuple[bytes, ...]) -> bytes:
    data = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1))
    for index, frame in enumerate(frames):
        data.extend(struct.pack("<IIII", 1 + index, 0, len(frame), len(frame)))
        data.extend(frame)
    return bytes(data)


def _base_capture() -> bytes:
    first = b"\x00\x05\x01AB"
    second = b"\x00\x05\x02CD"
    return _pcap(
        (
            _tcp_frame(sequence=100, flags=0x02),
            _tcp_frame(sequence=101, flags=0x18, payload=first + second),
        )
    )


def _tls_capture() -> bytes:
    tls_record = b"\x17\x03\x03\x00\x04\xde\xad\xbe\xef"
    return _pcap(
        (
            _tcp_frame(sequence=100, flags=0x02),
            _tcp_frame(sequence=101, flags=0x18, payload=tls_record),
        )
    )


def _workbench(content: bytes) -> tuple[NetworkWorkbench, _CaptureBlobs]:
    class _Tshark:
        available = False

    blobs = _CaptureBlobs()
    workbench = NetworkWorkbench(
        quarantine=_Quarantine(content),  # type: ignore[arg-type]
        blobs=blobs,  # type: ignore[arg-type]
        tshark=_Tshark(),  # type: ignore[arg-type]
        limits=NetworkExtractionLimitsV1(),
    )
    return workbench, blobs


def _messages_capture(*, endian: str, first_opcode: int = 1, second_opcode: int = 2) -> bytes:
    prefix = (3).to_bytes(2, endian)
    messages = prefix + bytes((first_opcode,)) + b"AB" + prefix + bytes((second_opcode,)) + b"CD"
    frames = (
        _tcp_frame(sequence=100, flags=0x02),
        _tcp_frame(sequence=101, flags=0x18, payload=messages),
    )
    return _pcap(frames)


def _one_message_capture() -> bytes:
    frames = (
        _tcp_frame(sequence=100, flags=0x02),
        _tcp_frame(sequence=101, flags=0x18, payload=b"\x00\x05\x01AB"),
    )
    return _pcap(frames)


async def _network_and_traces(
    captures: tuple[tuple[bytes, bool, str | None], ...],
) -> tuple[NetworkProtocolWorkbench, tuple[ProtocolTraceRequestV1, ...]]:
    workbench, _blobs = _workbench(_base_capture())
    traces: list[ProtocolTraceRequestV1] = []
    for index, (content, holdout, sample_sha256) in enumerate(captures):
        workbench.quarantine = _Quarantine(content)
        reference = await workbench.accept_quarantined_capture(
            f"quarantine:protocol:{index}",
            investigation_id=_INVESTIGATION_ID,
        )
        flow = _flows(read_capture(content).packets)[0]
        traces.append(
            ProtocolTraceRequestV1(
                capture=reference,
                flow_id=flow.flow_id,
                sample_sha256=sample_sha256,
                holdout=holdout,
            )
        )
    return NetworkProtocolWorkbench(workbench), tuple(traces)


def _proposal(*, first_expected: str = "01") -> ProtocolHypothesisProposalV1:
    first_message = "client_message_1"
    second_message = "client_message_2"
    first_field = "opcode_1"
    second_field = "opcode_2"
    return ProtocolHypothesisProposalV1(
        schema_version="ProtocolHypothesisProposalV1",
        messages=(
            HypothesisMessageV1(
                first_message,
                "client_handshake",
                1,
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                5,
                5,
            ),
            HypothesisMessageV1(
                second_message,
                "client_beacon",
                2,
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                5,
                5,
            ),
        ),
        fields=(
            HypothesisFieldV1(
                first_field,
                first_message,
                "opcode",
                2,
                1,
                None,
                "hex",
                None,
                first_expected,
            ),
            HypothesisFieldV1(
                second_field,
                second_message,
                "opcode",
                2,
                1,
                None,
                "hex",
                None,
                "02",
            ),
        ),
        transitions=(
            HypothesisTransitionV1(
                "transition_connect",
                "start",
                "connected",
                first_message,
                "message_received",
            ),
            HypothesisTransitionV1(
                "transition_beacon",
                "connected",
                "active",
                second_message,
                "message_received",
            ),
        ),
        test_plan=(
            ProtocolTestPlanStepV1(
                ProtocolTestKind.FIELD_VALUE, first_message, field_id=first_field
            ),
            ProtocolTestPlanStepV1(
                ProtocolTestKind.FIELD_VALUE, second_message, field_id=second_field
            ),
            ProtocolTestPlanStepV1(
                ProtocolTestKind.STATE_TRANSITION,
                first_message,
                transition_id="transition_connect",
            ),
            ProtocolTestPlanStepV1(
                ProtocolTestKind.STATE_TRANSITION,
                second_message,
                transition_id="transition_beacon",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_protocol_compare_is_deterministic_and_keeps_endianness_variants() -> None:
    sample_a = hashlib.sha256(b"sample-a").hexdigest()
    sample_b = hashlib.sha256(b"sample-b").hexdigest()
    service, traces = await _network_and_traces(
        (
            (_messages_capture(endian="big"), False, sample_a),
            (_messages_capture(endian="little"), False, sample_b),
        )
    )
    first = await service.compare(
        subject_id="sample-family-under-review",
        investigation_id=_INVESTIGATION_ID,
        traces=traces,
    )
    second = await service.compare(
        subject_id="sample-family-under-review",
        investigation_id=_INVESTIGATION_ID,
        traces=traces,
    )

    assert first.compatible is True
    assert first.model.canonical_sha256 == second.model.canonical_sha256
    assert first.model.family is None
    assert first.model.family_assurance is AssuranceLevel.UNCONFIRMED
    assert set(first.model.sample_sha256s) == {sample_a, sample_b}
    length_fields = [field for field in first.model.fields if field.name == "length_prefix"]
    assert {field.endianness for field in length_fields} == {"big", "little"}
    assert all(field.evidence for field in first.model.fields)
    sequence = next(
        item
        for item in service.render(first.model).diagrams
        if item.kind is ProtocolDiagramKind.SEQUENCE
    )
    assert sequence.status is ProtocolDiagramStatus.READY
    assert all(edge.frame_numbers for edge in sequence.edges)
    assert any(edge.timing_ms == 0 for edge in sequence.edges)
    restored = ProtocolModelV1.from_dict(first.model.to_dict())
    assert restored.canonical_sha256 == first.model.canonical_sha256
    proposal_dict = _proposal().to_dict()
    proposal_dict["free_text"] = "unstructured model output is rejected"
    with pytest.raises(ValueError):
        ProtocolHypothesisProposalV1.from_dict(proposal_dict)


@pytest.mark.asyncio
async def test_hypothesis_replays_then_holdout_contradiction_is_preserved() -> None:
    service, traces = await _network_and_traces(
        (
            (_messages_capture(endian="big"), False, hashlib.sha256(b"sample-a").hexdigest()),
            (_messages_capture(endian="little"), False, hashlib.sha256(b"sample-b").hexdigest()),
            (
                _messages_capture(endian="big", first_opcode=0x7F),
                True,
                hashlib.sha256(b"sample-c").hexdigest(),
            ),
        )
    )
    proposal = _proposal()
    model = await service.hypothesize(
        subject_id="subject-s2",
        investigation_id=_INVESTIGATION_ID,
        proposal=proposal,
        traces=traces[:2],
    )
    assert all(field.evidence for field in model.fields)
    assert all(transition.evidence for transition in model.transitions)
    assert model.test_plan == proposal.test_plan

    verified = await service.verify(model=model, traces=(traces[2],))
    opcode = next(field for field in verified.fields if field.field_id == "opcode_1")
    assert opcode.validation_status is ProtocolValidationStatus.CONTRADICTED
    assert any(
        item.item_id == "opcode_1"
        and item.validation_status is ProtocolValidationStatus.CONTRADICTED
        and item.evidence[0].frame_numbers
        for item in verified.verification_findings
    )
    assert verified.validation_status is ProtocolValidationStatus.CONTRADICTED

    later = await service.verify(
        model=verified,
        traces=(_replace_trace_holdout(traces[0]),),
    )
    assert any(
        item.item_id == "opcode_1"
        and item.validation_status is ProtocolValidationStatus.CONTRADICTED
        for item in later.verification_findings
    )
    assert next(
        field for field in later.fields if field.field_id == "opcode_1"
    ).validation_status is (ProtocolValidationStatus.CONTRADICTED)


@pytest.mark.asyncio
async def test_non_replayable_hypothesis_is_rejected_and_incomplete_path_unresolved() -> None:
    service, traces = await _network_and_traces(
        (
            (_messages_capture(endian="big"), False, None),
            (_messages_capture(endian="little"), False, None),
            (_one_message_capture(), True, None),
        )
    )
    with pytest.raises(HypothesisNotReplayableError):
        await service.hypothesize(
            subject_id="subject-s2",
            investigation_id=_INVESTIGATION_ID,
            proposal=_proposal(first_expected="ff"),
            traces=traces[:2],
        )
    model = await service.hypothesize(
        subject_id="subject-s2",
        investigation_id=_INVESTIGATION_ID,
        proposal=_proposal(),
        traces=traces[:2],
    )
    verified = await service.verify(model=model, traces=(traces[2],))
    assert verified.validation_status is ProtocolValidationStatus.UNRESOLVED
    assert any(
        item.kind in {"message_missing", "transition_missing"}
        and item.validation_status is ProtocolValidationStatus.UNRESOLVED
        for item in verified.verification_findings
    )


@pytest.mark.asyncio
async def test_opaque_encrypted_stream_and_insufficient_evidence_rendering() -> None:
    service, encrypted_traces = await _network_and_traces(((_tls_capture(), False, None),))
    content = _tls_capture()
    tls_flow = next(
        flow
        for flow in _flows(read_capture(content).packets)
        if flow.endpoint_a.port == 41000 or flow.endpoint_b.port == 41000
    )
    encrypted_request = _replace_trace_flow(encrypted_traces[0], tls_flow.flow_id)
    encrypted_model = await service.compare(
        subject_id="opaque-ciphertext",
        investigation_id=_INVESTIGATION_ID,
        traces=(encrypted_request,),
    )
    assert any(message.encrypted_opaque for message in encrypted_model.model.messages)
    assert all(
        field.expected_hex is None
        for field in encrypted_model.model.fields
        if field.encrypted_opaque
    )
    assert any("opaque" in item for item in encrypted_model.model.limitations)

    empty_service, empty_traces = await _network_and_traces(
        ((_one_message_capture(), False, None),)
    )
    empty = await empty_service.compare(
        subject_id="insufficient",
        investigation_id=_INVESTIGATION_ID,
        traces=empty_traces,
    )
    rendered = empty_service.render(empty.model)
    assert {item.kind for item in rendered.diagrams} == set(ProtocolDiagramKind)
    assert all(
        item.status is ProtocolDiagramStatus.INSUFFICIENT_EVIDENCE for item in rendered.diagrams
    )
    assert all(not item.nodes and not item.edges and not item.fields for item in rendered.diagrams)


def _replace_trace_flow(trace: ProtocolTraceRequestV1, flow_id: str) -> ProtocolTraceRequestV1:
    return ProtocolTraceRequestV1(
        capture=trace.capture,
        flow_id=flow_id,
        sample_sha256=trace.sample_sha256,
        report_id=trace.report_id,
        holdout=trace.holdout,
    )


def _replace_trace_holdout(trace: ProtocolTraceRequestV1) -> ProtocolTraceRequestV1:
    return ProtocolTraceRequestV1(
        capture=trace.capture,
        flow_id=trace.flow_id,
        sample_sha256=trace.sample_sha256,
        report_id=trace.report_id,
        holdout=True,
    )


def test_protocol_tools_register_exact_capability_keys_and_strict_input_schemas() -> None:
    assert protocol_tool_capabilities() == PROTOCOL_TOOL_NAMES
    assert PROTOCOL_TOOL_NAMES == {
        NETWORK_PROTOCOL_COMPARE,
        NETWORK_PROTOCOL_HYPOTHESIZE,
        NETWORK_PROTOCOL_VERIFY,
        NETWORK_PROTOCOL_RENDER,
    }
    workbench, _ = _workbench(_base_capture())
    registry = InMemoryToolRegistry()

    class _Recorder:
        async def record_json(self, **kwargs):
            raise AssertionError("registration must not record evidence")

    class _ModelStore:
        async def put_model(self, model):
            return uuid4(), model.canonical_sha256

        async def get_model(self, blob_id):
            raise AssertionError("registration must not read model blobs")

    register_protocol_tools(
        registry,
        service=NetworkProtocolWorkbench(workbench),
        recorder=_Recorder(),  # type: ignore[arg-type]
        model_store=_ModelStore(),  # type: ignore[arg-type]
    )
    for name in PROTOCOL_TOOL_NAMES:
        definition, _adapter = registry.get(name, "1")  # type: ignore[misc]
        assert definition.capability_key == name
        assert definition.input_schema["additionalProperties"] is False
        assert definition.confidentiality.value == "restricted"

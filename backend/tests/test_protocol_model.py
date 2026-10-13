from __future__ import annotations

import hashlib
import ipaddress
import json
import struct
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.application.investigation_protocol_tools import (
    NETWORK_PROTOCOL_COMPARE,
    NETWORK_PROTOCOL_HYPOTHESIZE,
    NETWORK_PROTOCOL_RENDER,
    NETWORK_PROTOCOL_VERIFY,
    PROTOCOL_TOOL_NAMES,
    ProductionProtocolModelStore,
    ProtocolModelNotInInvestigation,
    protocol_tool_capabilities,
    register_protocol_tools,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolExecutionContext,
)
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
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    EvidenceObservation,
    InvestigationBudget,
    ToolInvocationStatus,
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
    ProtocolRenderResultV1,
    ProtocolTestKind,
    ProtocolTestPlanStepV1,
    ProtocolValidationStatus,
)
from cti_app.infrastructure.pcap_reader import read_capture
from cti_app.infrastructure.tshark_adapter import TsharkAdapter

_INVESTIGATION_ID = UUID("2c2ae64e-a882-4cc0-b0cf-3bff6d111ba2")
_BLOB_NAMESPACE = UUID("ed0b9892-9b57-4f73-850a-0867e2230c60")


class _Quarantine:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def read_quarantined_capture(
        self, source_reference: str, *, max_bytes: int
    ) -> QuarantinedCapture:
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


class _EvidenceRecorder:
    def __init__(self) -> None:
        self.record_json_calls = 0

    async def record_json(
        self,
        *,
        investigation_id: UUID,
        kind: EvidenceKind,
        payload: Mapping[str, Any],
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...] = (),
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation:
        self.record_json_calls += 1
        encoded = json.dumps(dict(payload), sort_keys=True, default=str).encode()
        return EvidenceObservation(
            investigation_id=investigation_id,
            kind=kind,
            blob_id=uuid4(),
            sha256=hashlib.sha256(encoded).hexdigest(),
            source=source,
            source_version=source_version,
            observed_at=observed_at or datetime.now(UTC),
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
            invocation_ref=invocation_ref,
        )

    async def record_existing_blob(
        self,
        *,
        investigation_id: UUID,
        kind: EvidenceKind,
        blob_id: UUID,
        sha256: str,
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...] = (),
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation:
        return EvidenceObservation(
            investigation_id=investigation_id,
            kind=kind,
            blob_id=blob_id,
            sha256=sha256,
            source=source,
            source_version=source_version,
            observed_at=observed_at or datetime.now(UTC),
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
            invocation_ref=invocation_ref,
        )

    async def record_negative(
        self,
        *,
        investigation_id: UUID,
        reason: str,
        source: str,
        source_version: str,
        object_type: str,
        object_id: str,
        limitations: tuple[str, ...],
        details: Mapping[str, Any] | None = None,
        invocation_ref: UUID | None = None,
        observed_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> EvidenceObservation:
        return await self.record_json(
            investigation_id=investigation_id,
            kind=EvidenceKind.NEGATIVE_RESULT,
            payload={"reason": reason, "details": dict(details or {})},
            source=source,
            source_version=source_version,
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
            invocation_ref=invocation_ref,
            observed_at=observed_at,
            idempotency_key=idempotency_key,
        )


class _ProtocolModelStore:
    def __init__(self, model: ProtocolModelV1 | None = None) -> None:
        self.model = model
        self.blob_id = uuid4()
        self.get_calls = 0
        self.put_calls = 0

    async def put_model(self, model: ProtocolModelV1) -> tuple[UUID, str]:
        self.put_calls += 1
        self.model = model
        self.blob_id = uuid5(_BLOB_NAMESPACE, model.canonical_sha256)
        return self.blob_id, model.canonical_sha256

    async def get_model(self, blob_id: UUID, *, investigation_id: UUID) -> ProtocolModelV1:
        self.get_calls += 1
        if blob_id != self.blob_id or self.model is None:
            raise AssertionError("unknown protocol model blob")
        if self.model.investigation_id != investigation_id:
            raise ProtocolModelNotInInvestigation
        return self.model


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


def _bidirectional_capture() -> bytes:
    return _pcap(
        (
            _tcp_frame(sequence=100, flags=0x02),
            _tcp_frame(
                sequence=101,
                flags=0x18,
                payload=b"\x00\x05\x01AB\x00\x05\x02CD",
            ),
            _tcp_frame(
                source="203.0.113.10",
                destination="10.0.0.2",
                source_port=443,
                destination_port=41000,
                sequence=200,
                flags=0x18,
                payload=b"\x00\x05\x03ZY\x00\x05\x04XW",
            ),
        )
    )


def _workbench(content: bytes) -> tuple[NetworkWorkbench, _CaptureBlobs]:
    class _UnavailableTshark(TsharkAdapter):
        @property
        def available(self) -> bool:
            return False

    blobs = _CaptureBlobs()
    workbench = NetworkWorkbench(
        quarantine=_Quarantine(content),
        blobs=blobs,
        tshark=_UnavailableTshark(),
        limits=NetworkExtractionLimitsV1(),
    )
    return workbench, blobs


def _messages_capture(
    *,
    endian: str,
    first_opcode: int = 1,
    second_opcode: int = 2,
    initial_sequence: int = 100,
) -> bytes:
    prefix = (3).to_bytes(2, endian)
    messages = prefix + bytes((first_opcode,)) + b"AB" + prefix + bytes((second_opcode,)) + b"CD"
    frames = (
        _tcp_frame(sequence=initial_sequence, flags=0x02),
        _tcp_frame(sequence=initial_sequence + 1, flags=0x18, payload=messages),
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
async def test_candidate_stability_requires_two_independent_observations() -> None:
    one_service, one_trace = await _network_and_traces(
        ((_messages_capture(endian="big"), False, hashlib.sha256(b"sample-one").hexdigest()),)
    )
    one = await one_service.compare(
        subject_id="stability-one",
        investigation_id=_INVESTIGATION_ID,
        traces=one_trace,
    )
    candidate_fields = [
        field for field in one.model.fields if field.source == "network.messages.segment"
    ]
    assert candidate_fields
    assert all(field.stability.value == "unknown" for field in candidate_fields)
    assert not one.stable_field_ids

    identical_service, identical_traces = await _network_and_traces(
        (
            (
                _messages_capture(endian="big", initial_sequence=100),
                False,
                hashlib.sha256(b"identical-sample-one").hexdigest(),
            ),
            (
                _messages_capture(endian="big", initial_sequence=300),
                False,
                hashlib.sha256(b"identical-sample-two").hexdigest(),
            ),
        )
    )
    identical = await identical_service.compare(
        subject_id="stability-identical",
        investigation_id=_INVESTIGATION_ID,
        traces=identical_traces,
    )
    assert any(
        field.name == "length_prefix" and field.stability.value == "stable"
        for field in identical.model.fields
    )

    divergent_service, divergent_traces = await _network_and_traces(
        (
            (
                _messages_capture(endian="big", initial_sequence=100),
                False,
                hashlib.sha256(b"divergent-sample-one").hexdigest(),
            ),
            (
                _messages_capture(endian="big", first_opcode=0x7F, initial_sequence=300),
                False,
                hashlib.sha256(b"divergent-sample-two").hexdigest(),
            ),
        )
    )
    divergent = await divergent_service.compare(
        subject_id="stability-divergent",
        investigation_id=_INVESTIGATION_ID,
        traces=divergent_traces,
    )
    first_message_fields = [
        field
        for field in divergent.model.fields
        if field.name == "first_body_byte" and field.message_id.endswith("_1")
    ]
    assert len(first_message_fields) == 1
    assert first_message_fields[0].stability.value == "variant"
    assert first_message_fields[0].field_id in divergent.variant_field_ids


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
    training_capture_hashes = {trace.capture.sha256 for trace in traces[:2]}
    for proof in (
        evidence for item in (*model.fields, *model.transitions) for evidence in item.evidence
    ):
        assert proof.capture_sha256 in training_capture_hashes
        assert proof.stream_id
        assert proof.frame_numbers
        assert proof.start_offset is not None
        assert proof.end_offset is not None
    for item in (*model.fields, *model.transitions):
        assert {proof.capture_sha256 for proof in item.evidence} == training_capture_hashes

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
async def test_hypothesis_conversations_keep_each_observed_direction() -> None:
    service, traces = await _network_and_traces(((_bidirectional_capture(), False, None),))
    proposal = ProtocolHypothesisProposalV1(
        schema_version="ProtocolHypothesisProposalV1",
        messages=(
            HypothesisMessageV1(
                "client_message",
                "client_message",
                1,
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                5,
                5,
            ),
            HypothesisMessageV1(
                "client_message_2",
                "client_message_2",
                2,
                StreamDirection.ORIGINATOR_TO_RESPONDER,
                5,
                5,
            ),
            HypothesisMessageV1(
                "server_message",
                "server_message",
                1,
                StreamDirection.RESPONDER_TO_ORIGINATOR,
                5,
                5,
            ),
            HypothesisMessageV1(
                "server_message_2",
                "server_message_2",
                2,
                StreamDirection.RESPONDER_TO_ORIGINATOR,
                5,
                5,
            ),
        ),
        fields=(),
        transitions=(),
        test_plan=(),
    )

    model = await service.hypothesize(
        subject_id="bidirectional-subject",
        investigation_id=_INVESTIGATION_ID,
        proposal=proposal,
        traces=traces,
    )

    conversations = {item.direction: item for item in model.conversations}
    client_to_server = conversations[StreamDirection.ORIGINATOR_TO_RESPONDER]
    server_to_client = conversations[StreamDirection.RESPONDER_TO_ORIGINATOR]
    assert (client_to_server.source.address, client_to_server.source.port) == ("10.0.0.2", 41000)
    assert (client_to_server.destination.address, client_to_server.destination.port) == (
        "203.0.113.10",
        443,
    )
    assert client_to_server.message_ids == ("client_message", "client_message_2")
    assert (server_to_client.source.address, server_to_client.source.port) == (
        "203.0.113.10",
        443,
    )
    assert (server_to_client.destination.address, server_to_client.destination.port) == (
        "10.0.0.2",
        41000,
    )
    assert server_to_client.message_ids == ("server_message", "server_message_2")


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
    assert (
        next(
            item for item in verified.messages if item.message_id == "client_message_2"
        ).validation_status
        is ProtocolValidationStatus.UNRESOLVED
    )
    assert (
        next(item for item in verified.fields if item.field_id == "opcode_2").validation_status
        is ProtocolValidationStatus.UNRESOLVED
    )
    assert (
        next(
            item for item in verified.transitions if item.transition_id == "transition_beacon"
        ).validation_status
        is ProtocolValidationStatus.UNRESOLVED
    )
    assert any(
        item.kind in {"message_missing", "transition_missing"}
        and item.validation_status is ProtocolValidationStatus.UNRESOLVED
        for item in verified.verification_findings
    )
    rendered = service.render(verified)
    sequence = next(item for item in rendered.diagrams if item.kind is ProtocolDiagramKind.SEQUENCE)
    state_machine = next(
        item for item in rendered.diagrams if item.kind is ProtocolDiagramKind.STATE_MACHINE
    )
    message_format = next(
        item for item in rendered.diagrams if item.kind is ProtocolDiagramKind.MESSAGE_FORMAT
    )
    assert next(
        edge for edge in sequence.edges if edge.label == "client_beacon"
    ).validation_status is (ProtocolValidationStatus.UNRESOLVED)
    assert (
        next(
            edge for edge in state_machine.edges if edge.label.endswith("client_beacon")
        ).validation_status
        is ProtocolValidationStatus.UNRESOLVED
    )
    assert (
        next(
            field for field in message_format.fields if field.field_id == "opcode_2"
        ).validation_status
        is ProtocolValidationStatus.UNRESOLVED
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
    register_protocol_tools(
        registry,
        service=NetworkProtocolWorkbench(workbench),
        recorder=_EvidenceRecorder(),
        model_store=_ProtocolModelStore(),
    )
    for name in PROTOCOL_TOOL_NAMES:
        registration = registry.get(name, "1")
        assert registration is not None
        definition, _adapter = registration
        assert definition.capability_key == name
        assert definition.input_schema["additionalProperties"] is False
        assert definition.confidentiality.value == "restricted"


@pytest.mark.asyncio
async def test_protocol_render_tool_returns_existing_observation_uuid() -> None:
    service, traces = await _network_and_traces(((_messages_capture(endian="big"), False, None),))
    model = (
        await service.compare(
            subject_id="render-subject",
            investigation_id=_INVESTIGATION_ID,
            traces=traces,
        )
    ).model
    recorder = _EvidenceRecorder()
    model_store = _ProtocolModelStore(model)
    registry = InMemoryToolRegistry()
    workbench, _ = _workbench(_base_capture())
    register_protocol_tools(
        registry,
        service=NetworkProtocolWorkbench(workbench),
        recorder=recorder,
        model_store=model_store,
    )
    registration = registry.get(NETWORK_PROTOCOL_RENDER, "1")
    assert registration is not None
    _definition, adapter = registration
    context = ToolExecutionContext(
        investigation_id=_INVESTIGATION_ID,
        envelope=None,
        budget=InvestigationBudget({BudgetDimension.TOOL_SECONDS: 30}),
        idempotency_key="protocol-render-observation-test",
        available_capabilities=frozenset({NETWORK_PROTOCOL_RENDER}),
    )

    result = await adapter.execute({"model_blob_id": str(model_store.blob_id)}, context)

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert len(result.observation_refs) == 1
    observation_ref = result.observation_refs[0]
    assert isinstance(observation_ref, UUID)
    assert result.output["observation_ref"] == str(observation_ref)


@pytest.mark.parametrize(
    "tool_name",
    (NETWORK_PROTOCOL_VERIFY, NETWORK_PROTOCOL_RENDER),
)
@pytest.mark.asyncio
async def test_protocol_tools_reject_model_from_another_investigation_without_effects(
    tool_name: str,
) -> None:
    foreign_investigation_id = UUID("7731fc0d-b4c7-4a80-a7c0-dd87d3b258c8")
    model_service, traces = await _network_and_traces(
        ((_messages_capture(endian="big"), False, None),)
    )
    owned_model = (
        await model_service.compare(
            subject_id="foreign-model",
            investigation_id=_INVESTIGATION_ID,
            traces=traces,
        )
    ).model
    foreign_model = replace(owned_model, investigation_id=foreign_investigation_id)

    class _NoEffectService(NetworkProtocolWorkbench):
        def __init__(self, workbench: NetworkWorkbench) -> None:
            super().__init__(workbench)
            self.verify_calls = 0
            self.render_calls = 0

        async def verify(
            self,
            *,
            model: ProtocolModelV1,
            traces: Sequence[ProtocolTraceRequestV1],
            investigation_id: UUID | None = None,
        ) -> ProtocolModelV1:
            self.verify_calls += 1
            raise AssertionError("foreign model must be rejected before verification")

        def render(self, model: ProtocolModelV1) -> ProtocolRenderResultV1:
            self.render_calls += 1
            raise AssertionError("foreign model must be rejected before rendering")

    workbench, _ = _workbench(_base_capture())
    service = _NoEffectService(workbench)
    recorder = _EvidenceRecorder()

    class _ModelArtifacts(ProductionArtifactStore):
        def __init__(self, model: ProtocolModelV1) -> None:
            self.payload = model.to_dict()
            self.read_calls = 0
            self.put_calls = 0

        async def read_json(self, blob_id: UUID) -> dict[str, Any]:
            self.read_calls += 1
            return self.payload

        async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
            self.put_calls += 1
            return uuid4()

    artifacts = _ModelArtifacts(foreign_model)
    model_store = ProductionProtocolModelStore(artifacts)
    registry = InMemoryToolRegistry()
    register_protocol_tools(
        registry,
        service=service,
        recorder=recorder,
        model_store=model_store,
    )
    registration = registry.get(tool_name, "1")
    assert registration is not None
    _definition, adapter = registration
    context = ToolExecutionContext(
        investigation_id=_INVESTIGATION_ID,
        envelope=None,
        budget=InvestigationBudget({BudgetDimension.TOOL_SECONDS: 30}),
        idempotency_key=f"foreign-model-{tool_name}",
        available_capabilities=frozenset({tool_name}),
    )
    arguments: dict[str, object] = {"model_blob_id": str(uuid4())}
    if tool_name == NETWORK_PROTOCOL_VERIFY:
        arguments["traces"] = []

    result = await adapter.execute(arguments, context)

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output == {
        "detail_code": "protocol_model_not_in_investigation",
        "status": ToolInvocationStatus.POLICY_DENIED.value,
    }
    assert result.observation_refs == ()
    assert artifacts.read_calls == 1
    assert artifacts.put_calls == 0
    assert recorder.record_json_calls == 0
    assert service.verify_calls == 0
    assert service.render_calls == 0

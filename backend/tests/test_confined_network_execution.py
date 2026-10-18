from __future__ import annotations

import hashlib
import ipaddress
import json
import shutil
import struct
import subprocess
from collections.abc import Mapping
from io import BytesIO
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.blob_storage import BlobReadLimitExceededError
from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisRequest,
    ConfinedAnalysisResult,
    ConfinedAnalysisTool,
    ConfinedAnalysisWorkerAttestation,
    deterministic_confined_analysis_key,
)
from cti_app.application.jobs import JobExecutionContext
from cti_app.application.network_workbench import (
    NetworkWorkbench,
    QuarantinedCapture,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.network_protocol import NetworkExtractionLimitsV1
from cti_app.infrastructure.confined_tshark import ConfinedTsharkAdapter
from cti_app.workers.analysis_tasks import build_confined_analysis_job_handler
from cti_app.workers.confined_analysis import (
    ConfinedAnalysisArtifactMismatch,
    _validate_command_arguments,
)


class _MemoryUnitOfWork:
    def __init__(self, database: _MemoryDatabase) -> None:
        self.database = database
        self.blobs = self
        self.analyst_investigations = self
        self.evidence_observations = self

    async def __aenter__(self) -> _MemoryUnitOfWork:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, identity: UUID) -> Any:
        if identity in self.database.blobs.records:
            return self.database.blobs.records[identity]
        if identity == self.database.investigation_id:
            return self.database.investigation
        return None

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        if investigation_id != self.database.investigation_id:
            return ()
        return tuple(self.database.observations)


class _MemoryDatabase:
    def __init__(self) -> None:
        self.investigation_id = uuid4()
        self.investigation = object()
        self.observations: list[Any] = []
        self.blobs = _MemoryBlobCatalog()

    def uow(self) -> _MemoryUnitOfWork:
        return _MemoryUnitOfWork(self)


class _MemoryBlobCatalog:
    def __init__(self) -> None:
        self.records: dict[UUID, BlobRecord] = {}
        self.contents: dict[UUID, bytes] = {}

    async def ingest(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobRecord:
        content = source.read()
        descriptor = BlobDescriptor(
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            mime_type=mime_type,
            logical_bucket=logical_bucket,
        )
        record = BlobRecord(descriptor=descriptor)
        self.records[record.id] = record
        self.contents[record.id] = content
        return record

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self.contents[blob_id]
        if len(content) > max_bytes:
            raise BlobReadLimitExceededError(size_bytes=len(content), max_bytes=max_bytes)
        return content


class _CaptureQuarantine:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def read_quarantined_capture(self, source_reference: str, *, max_bytes: int):
        assert source_reference == "fixture:synthetic"
        assert len(self.content) <= max_bytes
        return QuarantinedCapture(
            source_reference,
            self.content,
            hashlib.sha256(self.content).hexdigest(),
        )


class _CaptureBlobStore:
    def __init__(self, blobs: _MemoryBlobCatalog) -> None:
        self.blobs = blobs

    async def put_capture(self, content: bytes, *, mime_type: str) -> UUID:
        record = await self.blobs.ingest(
            BytesIO(content),
            logical_bucket="network-captures-quarantine",
            mime_type=mime_type,
        )
        return record.id

    async def read_capture(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        return await self.blobs.read(blob_id, max_bytes=max_bytes)


class _EvidenceRecorder:
    def __init__(self, database: _MemoryDatabase) -> None:
        self.database = database

    async def record_existing_blob(self, **values: Any) -> None:
        self.database.observations.append(
            type(
                "CaptureObservation",
                (),
                {
                    "blob_id": values["blob_id"],
                    "sha256": values["sha256"],
                    "source": values["source"],
                    "object_type": values["object_type"],
                },
            )()
        )


class _InProcessWorkerPort:
    def __init__(
        self,
        *,
        blobs: _MemoryBlobCatalog,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
    ) -> None:
        self._blobs = blobs
        self._handler = build_confined_analysis_job_handler(
            blobs=cast(Any, blobs), uow_factory=uow_factory, settings=settings
        )
        self._versions = {
            "tshark": _binary_version("tshark", "--version", "TShark"),
            "capinfos": _binary_version("capinfos", "-v", "Capinfos"),
        }
        self._families = {
            name: ConfinedAnalysisFamilyStatus(name, True, version)
            for name, version in self._versions.items()
        }
        self._attestation = ConfinedAnalysisWorkerAttestation(
            reachable=True,
            confined=True,
            isolation_profile="test-in-process",
            versions=self._versions,
            families=self._families,
            dependencies_ready=True,
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def available_reason(self) -> str | None:
        return None

    @property
    def family_statuses(self) -> Mapping[str, ConfinedAnalysisFamilyStatus]:
        return self._families

    @property
    def isolation_profile(self) -> str:
        return "test-in-process"

    @property
    def attestation(self) -> ConfinedAnalysisWorkerAttestation:
        return self._attestation

    async def execute(self, request: ConfinedAnalysisRequest) -> ConfinedAnalysisResult:
        operation_key = deterministic_confined_analysis_key(request, worker_versions=self._versions)
        parameters = ConfinedAnalysisJobParameters(
            investigation_id=request.investigation_id,
            association_id=request.association_id,
            sample_id=request.sample_id,
            artifact_id=request.artifact_id,
            artifact_sha256=request.sha256,
            artifact_kind=request.artifact_kind,
            operation_key=operation_key,
            tool=request.tool,
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
            arguments=request.arguments,
        )
        output_reference = await self._handler(parameters, cast(JobExecutionContext, object()))
        match = output_reference.removeprefix("confined-analysis-result://").split("/")
        result_blob_id = UUID(match[0])
        result_sha256 = match[1]
        payload_bytes = await self._blobs.read(
            result_blob_id,
            max_bytes=request.max_output_bytes,
        )
        assert hashlib.sha256(payload_bytes).hexdigest() == result_sha256
        payload = json.loads(payload_bytes)
        return ConfinedAnalysisResult(
            status=ToolInvocationStatus.SUCCEEDED,
            operation_key=operation_key,
            artifact_id=request.artifact_id,
            input_sha256=request.sha256,
            result_blob_id=result_blob_id,
            result_sha256=result_sha256,
            payload=payload,
        )


def _binary_version(binary: str, argument: str, product: str) -> str:
    result = subprocess.run(
        (binary, argument),
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    line = next((line for line in result.stdout.splitlines() if product in line), "")
    version = line.rsplit(" ", 1)[-1]
    assert version and version[0].isdigit()
    return version


def _tcp_frame(
    *,
    source_port: int,
    destination_port: int,
    payload: bytes,
    sequence: int = 1,
) -> bytes:
    tcp_header = struct.pack(
        "!HHIIHHHH",
        source_port,
        destination_port,
        sequence,
        0,
        (5 << 12) | 0x18,
        65_535,
        0,
        0,
    )
    return _ethernet_ipv4(6, tcp_header + payload)


def _udp_frame(source_port: int, destination_port: int, payload: bytes) -> bytes:
    udp = struct.pack("!HHHH", source_port, destination_port, 8 + len(payload), 0) + payload
    return _ethernet_ipv4(17, udp)


def _ethernet_ipv4(protocol: int, payload: bytes) -> bytes:
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(payload),
        1,
        0,
        64,
        protocol,
        0,
        ipaddress.IPv4Address("192.0.2.10").packed,
        ipaddress.IPv4Address("192.0.2.53").packed,
    )
    return bytes.fromhex("00112233445566778899aabb0800") + ip_header + payload


def _dns_query(name: str) -> bytes:
    qname = b"".join(bytes((len(label),)) + label.encode("ascii") for label in name.split("."))
    qname += b"\x00"
    return struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", 1, 1)


def _tls_client_hello(server_name: str) -> bytes:
    name = server_name.encode("ascii")
    name_entry = b"\x00" + struct.pack("!H", len(name)) + name
    server_names = struct.pack("!H", len(name_entry)) + name_entry
    extension = b"\x00\x00" + struct.pack("!H", len(server_names)) + server_names
    extensions = struct.pack("!H", len(extension)) + extension
    hello_body = b"\x03\x03" + bytes(range(32)) + b"\x00\x00\x02\x13\x01\x01\x00" + extensions
    handshake = b"\x01" + len(hello_body).to_bytes(3, "big") + hello_body
    return b"\x16\x03\x01" + struct.pack("!H", len(handshake)) + handshake


def _synthetic_pcap() -> bytes:
    dns = _dns_query("telemetry.example.test")
    http = (
        b"GET /beacon HTTP/1.1\r\nHost: c2.example.test\r\nUser-Agent: autowork-synthetic\r\n\r\n"
    )
    custom_protocol = b"\x00\x05\x01ABCD\x00\x05\x02EFGH"
    frames = (
        _udp_frame(53000, 53, dns),
        _tcp_frame(source_port=41000, destination_port=80, payload=http),
        _tcp_frame(
            source_port=41001, destination_port=443, payload=_tls_client_hello("tls.example.test")
        ),
        _tcp_frame(source_port=41002, destination_port=9000, payload=custom_protocol),
    )
    data = bytearray(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1))
    for index, frame in enumerate(frames):
        data.extend(struct.pack("<IIII", 1 + index, 0, len(frame), len(frame)))
        data.extend(frame)
    return bytes(data)


def _missing_tools() -> tuple[str, ...]:
    return tuple(name for name in ("tshark", "capinfos") if shutil.which(name) is None)


@pytest.mark.asyncio
@pytest.mark.skipif(_missing_tools(), reason="real TShark and capinfos are required")
async def test_canonical_pcap_runs_through_worker_handler_and_extracts_network_iocs() -> None:
    database = _MemoryDatabase()
    uow_factory = cast(UnitOfWorkFactory, database.uow)
    settings = Settings(_env_file=None)
    worker_port = _InProcessWorkerPort(
        blobs=database.blobs,
        uow_factory=uow_factory,
        settings=settings,
    )
    limits = NetworkExtractionLimitsV1(max_packets=20, max_output_bytes=64 * 1024)
    adapter = ConfinedTsharkAdapter(
        worker_port,
        limits=limits,
        max_output_bytes=64 * 1024,
    )
    workbench = NetworkWorkbench(
        quarantine=_CaptureQuarantine(_synthetic_pcap()),
        blobs=_CaptureBlobStore(database.blobs),
        tshark=adapter,
        recorder=_EvidenceRecorder(database),
        limits=limits,
    )
    reference = await workbench.accept_quarantined_capture(
        "fixture:synthetic",
        investigation_id=database.investigation_id,
    )

    outcome = await workbench.inspect(reference, investigation_id=database.investigation_id)

    assert outcome.status is ToolInvocationStatus.SUCCEEDED
    assert outcome.report is not None
    report = outcome.report.to_dict()
    observed_values = {
        str(item["value"])
        for key in ("dns_observations", "http_observations", "tls_observations")
        for item in report[key]
        if isinstance(item, dict)
    }
    assert {
        "telemetry.example.test",
        "c2.example.test",
        "/beacon",
        "tls.example.test",
    } <= observed_values
    assert worker_port.attestation.reachable
    assert all(status.available for status in worker_port.family_statuses.values())
    assert len(database.observations) == 1


def test_tshark_command_rejects_non_allowlisted_fields_and_lua_extensions() -> None:
    with pytest.raises(ValueError, match="not allowlisted"):
        _validate_command_arguments("tshark", ("-T", "fields", "-e", "frame.interface_id"))
    with pytest.raises(ValueError, match="not allowlisted"):
        _validate_command_arguments("tshark", ("-X", "lua_script:untrusted.lua"))
    with pytest.raises(ValueError, match="not allowlisted"):
        _validate_command_arguments("tshark", ("--only-protocols", "http"))


@pytest.mark.asyncio
async def test_capture_worker_rejects_unassociated_or_non_capture_blob() -> None:
    database = _MemoryDatabase()
    capture = await database.blobs.ingest(
        BytesIO(_synthetic_pcap()),
        logical_bucket="confined-analysis-results",
        mime_type="application/json",
    )
    settings = Settings(_env_file=None)
    handler = build_confined_analysis_job_handler(
        blobs=cast(Any, database.blobs),
        uow_factory=cast(UnitOfWorkFactory, database.uow),
        settings=settings,
    )
    params = ConfinedAnalysisJobParameters(
        investigation_id=database.investigation_id,
        artifact_id=capture.id,
        artifact_sha256=capture.descriptor.sha256,
        artifact_kind=ConfinedAnalysisArtifactKind.CAPTURE,
        operation_key=f"confined-analysis:{'a' * 64}",
        tool=ConfinedAnalysisTool.TSHARK,
        timeout_seconds=10,
        max_output_bytes=64 * 1024,
        arguments=("-T", "fields", "-c", "10", "-e", "frame.number"),
    )

    with pytest.raises(ConfinedAnalysisArtifactMismatch, match="analysis_capture_not_associated"):
        await handler(params, cast(JobExecutionContext, object()))

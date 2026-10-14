from __future__ import annotations

import hashlib
import struct
from types import SimpleNamespace
from uuid import UUID, uuid5

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api.investigation_captures import router
from cti_app.application.blob_storage import BlobReadLimitExceededError
from cti_app.application.network_capture_import import (
    CaptureInvestigationNotFoundError,
    NetworkCaptureImportService,
)
from cti_app.application.network_workbench import (
    NetworkCaptureRejected,
    QuarantineCaptureTooLargeError,
)
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.investigation import EvidenceObservation
from cti_app.domain.network_protocol import NetworkCaptureFormat, NetworkCaptureRejection
from cti_app.infrastructure.quarantined_capture_source import (
    ProductionQuarantinedCaptureSource,
)

_INVESTIGATION_ID = UUID("0a92407c-bcba-40b4-90dc-1d0a0907494c")
_OTHER_INVESTIGATION_ID = UUID("35b70ac1-8299-49cf-9cc0-89ca49ec7882")
_BLOB_NAMESPACE = UUID("444b87de-dc37-46af-a5e9-3774b034cc1e")
_PCAP_HEADER = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1)
_PCAPNG_HEADER = (
    b"\x0a\x0d\x0d\x0a"
    + struct.pack("<I", 28)
    + b"\x4d\x3c\x2b\x1a"
    + struct.pack("<HHqI", 1, 0, -1, 28)
)


def test_api_root_mounts_capture_import_routes() -> None:
    from cti_app.api.main import create_app

    paths = set(create_app().openapi()["paths"])

    assert "/api/v1/network-captures/capabilities" in paths
    assert "/api/v1/investigations/{investigation_id}/network-captures" in paths


class _EvidenceRepository:
    def __init__(self) -> None:
        self.observations: dict[UUID, EvidenceObservation] = {}

    async def get(self, observation_id: UUID) -> EvidenceObservation | None:
        return self.observations.get(observation_id)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return tuple(
            item for item in self.observations.values() if item.investigation_id == investigation_id
        )

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation:
        same_identity = next(
            (
                item
                for item in self.observations.values()
                if item.investigation_id == observation.investigation_id
                and item.sha256 == observation.sha256
                and item.kind == observation.kind
                and item.object_type == observation.object_type
                and item.object_id == observation.object_id
            ),
            None,
        )
        if same_identity is not None:
            return same_identity
        self.observations[observation.id] = observation
        return observation


class _BlobCatalog:
    def __init__(self) -> None:
        self.records: dict[UUID, BlobRecord] = {}
        self.content_by_id: dict[UUID, bytes] = {}
        self.addresses: dict[tuple[str, str], UUID] = {}
        self.ingest_calls = 0

    async def ingest(self, source, *, logical_bucket: str, mime_type: str) -> BlobRecord:
        self.ingest_calls += 1
        content = source.read()
        sha256 = hashlib.sha256(content).hexdigest()
        address = (logical_bucket, sha256)
        existing_id = self.addresses.get(address)
        if existing_id is not None:
            return self.records[existing_id]
        blob_id = uuid5(_BLOB_NAMESPACE, f"{logical_bucket}:{sha256}")
        record = BlobRecord(
            id=blob_id,
            descriptor=BlobDescriptor(
                sha256=sha256,
                size=len(content),
                mime_type=mime_type,
                logical_bucket=logical_bucket,
            ),
        )
        self.records[blob_id] = record
        self.content_by_id[blob_id] = content
        self.addresses[address] = blob_id
        return record

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self.content_by_id[blob_id]
        if len(content) > max_bytes:
            raise BlobReadLimitExceededError(size_bytes=len(content), max_bytes=max_bytes)
        return content

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self.records.get(blob_id)


class _InvestigationRepository:
    def __init__(self, ids: set[UUID]) -> None:
        self.ids = ids

    async def get(self, investigation_id: UUID):
        return object() if investigation_id in self.ids else None


class _UnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.analyst_investigations = _InvestigationRepository(harness.investigation_ids)
        self.evidence_observations = harness.evidence
        self.blobs = harness.blobs
        self.commits = 0

    async def __aenter__(self) -> _UnitOfWork:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _UnitOfWorkFactory:
    def __init__(self, harness: _Harness) -> None:
        self.harness = harness

    def __call__(self) -> _UnitOfWork:
        return _UnitOfWork(self.harness)


class _Harness:
    def __init__(self, *, max_bytes: int = 64 * 1024) -> None:
        self.investigation_ids = {_INVESTIGATION_ID}
        self.evidence = _EvidenceRepository()
        self.blobs = _BlobCatalog()
        self.uow_factory = _UnitOfWorkFactory(self)
        self.settings = SimpleNamespace(network_capture_max_bytes=max_bytes)
        self.service = NetworkCaptureImportService(
            uow_factory=self.uow_factory,  # type: ignore[arg-type]
            blob_catalog=self.blobs,  # type: ignore[arg-type]
            settings=self.settings,  # type: ignore[arg-type]
        )


async def _import(harness: _Harness, content: bytes = _PCAP_HEADER):
    return await harness.service.import_capture(
        investigation_id=_INVESTIGATION_ID,
        content=content,
        operator_id="analyst:unit-test",
    )


@pytest.mark.asyncio
async def test_import_rejects_malformed_capture() -> None:
    harness = _Harness()

    with pytest.raises(NetworkCaptureRejected) as rejected:
        await _import(harness, b"not-a-pcap")

    assert rejected.value.reason is NetworkCaptureRejection.INVALID_FORMAT
    assert harness.blobs.ingest_calls == 0


@pytest.mark.asyncio
async def test_import_rejects_capture_over_configured_limit() -> None:
    harness = _Harness(max_bytes=len(_PCAP_HEADER))

    with pytest.raises(NetworkCaptureRejected) as rejected:
        await _import(harness, _PCAP_HEADER + b"x")

    assert rejected.value.reason is NetworkCaptureRejection.SIZE_LIMIT
    assert harness.blobs.ingest_calls == 0


@pytest.mark.asyncio
async def test_import_rejects_declared_sha256_mismatch() -> None:
    harness = _Harness()

    with pytest.raises(NetworkCaptureRejected) as rejected:
        await harness.service.import_capture(
            investigation_id=_INVESTIGATION_ID,
            content=_PCAP_HEADER,
            operator_id="analyst:unit-test",
            declared_sha256="0" * 64,
        )

    assert rejected.value.reason is NetworkCaptureRejection.BLOB_HASH_MISMATCH
    assert harness.blobs.ingest_calls == 0


@pytest.mark.asyncio
async def test_import_accepts_pcapng_section_magic() -> None:
    harness = _Harness()

    imported = await _import(harness, _PCAPNG_HEADER)

    assert imported.format is NetworkCaptureFormat.PCAPNG
    assert imported.size_bytes == len(_PCAPNG_HEADER)


@pytest.mark.asyncio
async def test_import_requires_existing_exact_investigation() -> None:
    harness = _Harness()

    with pytest.raises(CaptureInvestigationNotFoundError):
        await harness.service.import_capture(
            investigation_id=_OTHER_INVESTIGATION_ID,
            content=_PCAP_HEADER,
            operator_id="analyst:unit-test",
        )

    assert harness.blobs.ingest_calls == 0


@pytest.mark.asyncio
async def test_duplicate_import_reuses_one_blob_and_one_evidence_observation() -> None:
    harness = _Harness()

    first = await _import(harness)
    second = await _import(harness)

    assert first.blob_id == second.blob_id
    assert first.sha256 == hashlib.sha256(_PCAP_HEADER).hexdigest()
    assert first.source_reference == second.source_reference
    assert len(harness.blobs.records) == 1
    assert len(harness.evidence.observations) == 1
    observation = harness.evidence.observations[next(iter(harness.evidence.observations))]
    assert "analyst:unit-test" in observation.limitations[1]


@pytest.mark.asyncio
async def test_quarantine_source_returns_imported_bytes_and_digest() -> None:
    harness = _Harness()
    imported = await _import(harness)
    source = ProductionQuarantinedCaptureSource(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        blob_catalog=harness.blobs,  # type: ignore[arg-type]
        settings=harness.settings,  # type: ignore[arg-type]
        investigation_id=_INVESTIGATION_ID,
    )

    with pytest.raises(QuarantineCaptureTooLargeError):
        await source.read_quarantined_capture(
            imported.source_reference, max_bytes=len(_PCAP_HEADER) - 1
        )
    capture = await source.read_quarantined_capture(
        imported.source_reference,
        max_bytes=len(_PCAP_HEADER),
    )

    assert capture.content == _PCAP_HEADER
    assert capture.sha256 == hashlib.sha256(_PCAP_HEADER).hexdigest()
    assert capture.source_reference == imported.source_reference


@pytest.mark.asyncio
async def test_quarantine_source_rejects_another_investigation_and_wrong_sha() -> None:
    harness = _Harness()
    imported = await _import(harness)
    source = ProductionQuarantinedCaptureSource(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        blob_catalog=harness.blobs,  # type: ignore[arg-type]
        settings=harness.settings,  # type: ignore[arg-type]
        investigation_id=_OTHER_INVESTIGATION_ID,
    )

    with pytest.raises(NetworkCaptureRejected) as wrong_investigation:
        await source.read_quarantined_capture(imported.source_reference, max_bytes=1024)
    assert wrong_investigation.value.reason is NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED

    source._investigation_id = _INVESTIGATION_ID
    with pytest.raises(NetworkCaptureRejected) as wrong_sha:
        await source.read_quarantined_capture(f"sha256:{'f' * 64}", max_bytes=1024)
    assert wrong_sha.value.reason is NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED


@pytest.mark.asyncio
async def test_quarantine_source_rejects_blob_hash_tampering() -> None:
    harness = _Harness()
    imported = await _import(harness)
    harness.blobs.content_by_id[imported.blob_id] = b"tampered"
    source = ProductionQuarantinedCaptureSource(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        blob_catalog=harness.blobs,  # type: ignore[arg-type]
        settings=harness.settings,  # type: ignore[arg-type]
        investigation_id=_INVESTIGATION_ID,
    )

    with pytest.raises(NetworkCaptureRejected) as rejected:
        await source.read_quarantined_capture(imported.source_reference, max_bytes=1024)

    assert rejected.value.reason is NetworkCaptureRejection.BLOB_HASH_MISMATCH


@pytest.mark.asyncio
async def test_import_route_requires_operator_identity() -> None:
    harness = _Harness()
    app = FastAPI()
    app.include_router(router)
    app.state.network_capture_import_service = harness.service

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/investigations/{_INVESTIGATION_ID}/network-captures",
            content=_PCAP_HEADER,
            headers={"content-type": "application/octet-stream"},
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "operator_identity_required"
    assert harness.blobs.ingest_calls == 0


@pytest.mark.asyncio
async def test_import_route_rejects_arbitrary_filesystem_path_payload() -> None:
    harness = _Harness()
    app = FastAPI()
    app.include_router(router)
    app.state.network_capture_import_service = harness.service
    app.state.identity_provider = SimpleNamespace(current=lambda: _identity("analyst:api-test"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/investigations/{_INVESTIGATION_ID}/network-captures",
            json={"path": "/etc/passwd"},
        )

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "capture_content_type_required"
    assert harness.blobs.ingest_calls == 0


@pytest.mark.asyncio
async def test_import_route_bounds_raw_upload_body() -> None:
    harness = _Harness(max_bytes=len(_PCAP_HEADER))
    app = FastAPI()
    app.include_router(router)
    app.state.network_capture_import_service = harness.service
    app.state.identity_provider = SimpleNamespace(current=lambda: _identity("analyst:api-test"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/investigations/{_INVESTIGATION_ID}/network-captures",
            content=_PCAP_HEADER + b"x",
            headers={"content-type": "application/octet-stream"},
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "size_limit"
    assert harness.blobs.ingest_calls == 0


@pytest.mark.asyncio
async def test_import_route_exposes_capabilities_and_imports_uploaded_content() -> None:
    harness = _Harness()
    app = FastAPI()
    app.include_router(router)
    app.state.network_capture_import_service = harness.service
    app.state.identity_provider = SimpleNamespace(current=lambda: _identity("analyst:api-test"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        capabilities = await client.get("/api/v1/network-captures/capabilities")
        response = await client.post(
            f"/api/v1/investigations/{_INVESTIGATION_ID}/network-captures",
            content=_PCAP_HEADER,
            headers={
                "content-type": "application/octet-stream",
                "x-content-sha256": hashlib.sha256(_PCAP_HEADER).hexdigest(),
            },
        )

    assert capabilities.status_code == 200
    assert capabilities.json()["schema_version"] == "network-capture-import-capabilities.v1"
    assert capabilities.json()["requires_operator_identity"] is True
    assert capabilities.json()["sha256_header"] == "X-Content-SHA256"
    assert response.status_code == 201
    assert response.json()["schema_version"] == "network-capture-import.v1"
    expected_reference = f"sha256:{hashlib.sha256(_PCAP_HEADER).hexdigest()}"
    assert response.json()["source_reference"] == expected_reference


async def _identity(actor_id: str):
    return SimpleNamespace(actor_id=actor_id)

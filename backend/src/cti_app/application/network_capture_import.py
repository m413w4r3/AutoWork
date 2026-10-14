"""Bounded operator import for inert PCAP/PCAPNG evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.network_workbench import (
    NetworkCaptureRejected,
    ProductionNetworkCaptureBlobStore,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.investigation import EvidenceKind
from cti_app.domain.network_protocol import (
    NetworkCaptureFormat,
    NetworkCaptureRejection,
    NetworkExtractionLimitsV1,
)
from cti_app.infrastructure.pcap_reader import PcapReaderError, read_capture

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMPORT_SOURCE_VERSION = "network-capture-import.v1"
_PCAP_MIME = {
    NetworkCaptureFormat.PCAP: "application/vnd.tcpdump.pcap",
    NetworkCaptureFormat.PCAPNG: "application/x-pcapng",
}


class CaptureInvestigationNotFoundError(LookupError):
    """The operator supplied an investigation id that does not exist."""


@dataclass(frozen=True, slots=True)
class ImportedNetworkCapture:
    investigation_id: UUID
    source_reference: str
    blob_id: UUID
    sha256: str
    size_bytes: int
    format: NetworkCaptureFormat


class NetworkCaptureImportService:
    """Store capture bytes in the quarantine bucket and bind them to an investigation."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        blob_catalog: BlobCatalogService,
        settings: Settings,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_catalog = blob_catalog
        self._settings = settings

    @property
    def max_upload_bytes(self) -> int:
        return capture_max_bytes(self._settings)

    async def import_capture(
        self,
        *,
        investigation_id: UUID,
        content: bytes,
        operator_id: str,
        declared_sha256: str | None = None,
    ) -> ImportedNetworkCapture:
        max_bytes = self.max_upload_bytes
        if not content:
            raise NetworkCaptureRejected(NetworkCaptureRejection.INVALID_FORMAT)
        if len(content) > max_bytes:
            raise NetworkCaptureRejected(NetworkCaptureRejection.SIZE_LIMIT)

        actual_sha256 = hashlib.sha256(content).hexdigest()
        if declared_sha256 is not None and (
            not _SHA256.fullmatch(declared_sha256) or declared_sha256 != actual_sha256
        ):
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_HASH_MISMATCH)

        try:
            parsed = read_capture(
                content,
                max_packets=NetworkExtractionLimitsV1().max_packets,
            )
        except PcapReaderError as exc:
            reason = (
                NetworkCaptureRejection.INVALID_FORMAT
                if exc.code in {"invalid_magic", "unsupported_link_type"}
                else NetworkCaptureRejection.PACKET_LIMIT
                if exc.code == "packet_limit"
                else NetworkCaptureRejection.CORRUPT_CAPTURE
            )
            raise NetworkCaptureRejected(reason) from exc

        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise CaptureInvestigationNotFoundError(str(investigation_id))

        artifact_store = ProductionArtifactStore(self._blob_catalog)
        capture_blobs = ProductionNetworkCaptureBlobStore(artifact_store)
        blob_id = await capture_blobs.put_capture(
            content,
            mime_type=_PCAP_MIME[parsed.format],
        )

        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise CaptureInvestigationNotFoundError(str(investigation_id))
            recorder = ProductionEvidenceRecorder(
                artifact_store=artifact_store,
                observations=uow.evidence_observations,
            )
            observation = await recorder.record_existing_blob(
                investigation_id=investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=blob_id,
                sha256=actual_sha256,
                source="network.quarantine",
                source_version=_IMPORT_SOURCE_VERSION,
                object_type="network_capture",
                object_id=actual_sha256,
                limitations=(
                    "Le PCAP/PCAPNG complet est conservé dans le blob immuable de quarantaine.",
                    f"Identité opérateur vérifiée lors de l'import : {operator_id}.",
                    "Le contenu est non fiable et n'est jamais exécuté.",
                ),
            )
            await uow.commit()

        return ImportedNetworkCapture(
            investigation_id=investigation_id,
            source_reference=f"sha256:{observation.sha256}",
            blob_id=observation.blob_id,
            sha256=observation.sha256,
            size_bytes=len(content),
            format=parsed.format,
        )


def capture_max_bytes(settings: Settings) -> int:
    """Honor a runtime override while respecting the network-workbench hard cap."""
    hard_limit = NetworkExtractionLimitsV1().max_capture_bytes
    configured = getattr(settings, "network_capture_max_bytes", hard_limit)
    if isinstance(configured, bool) or not isinstance(configured, int) or configured <= 0:
        raise ValueError("network_capture_max_bytes must be a positive integer")
    return min(configured, hard_limit)

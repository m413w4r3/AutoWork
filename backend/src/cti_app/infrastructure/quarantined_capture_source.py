"""Production quarantine source backed by PostgreSQL evidence and the blob catalog."""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

from cti_app.application.blob_storage import BlobReadLimitExceededError, BlobStorageUnavailableError
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.network_capture_import import capture_max_bytes
from cti_app.application.network_workbench import (
    _NETWORK_CAPTURE_BUCKET,
    NetworkCaptureRejected,
    QuarantineCaptureTooLargeError,
    QuarantinedCapture,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import Settings
from cti_app.domain.errors import EntityNotFoundError
from cti_app.domain.investigation import EvidenceKind
from cti_app.domain.network_protocol import (
    NetworkCaptureRejection,
    NetworkExtractionLimitsV1,
)
from cti_app.infrastructure.pcap_reader import PcapReaderError, read_capture

_CAPTURE_REFERENCE = re.compile(r"^sha256:([0-9a-f]{64})$")
_VT_SANDBOX_PCAP_SOURCE_VERSION = "vt-sandbox-pcap.v1"


class ProductionQuarantinedCaptureSource:
    """Resolve only PCAPs imported for the exact investigation this instance owns."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        blob_catalog: BlobCatalogService,
        settings: Settings,
        investigation_id: UUID,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_catalog = blob_catalog
        self._settings = settings
        self._investigation_id = investigation_id

    def is_bound_to_investigation(self, investigation_id: UUID) -> bool:
        """Report whether this resolver is scoped to the requested investigation."""
        return investigation_id == self._investigation_id

    async def read_quarantined_capture(
        self, source_reference: str, *, max_bytes: int
    ) -> QuarantinedCapture:
        match = _CAPTURE_REFERENCE.fullmatch(source_reference)
        if match is None:
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_HASH_MISMATCH)
        sha256 = match.group(1)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise QuarantineCaptureTooLargeError("Capture read limit must be positive")
        effective_max = min(max_bytes, capture_max_bytes(self._settings))

        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(self._investigation_id)
            if investigation is None:
                raise NetworkCaptureRejected(NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED)
            observations = await uow.evidence_observations.list_for_investigation(
                self._investigation_id
            )
            observation = next(
                (
                    item
                    for item in observations
                    if item.kind is EvidenceKind.OTHER
                    and item.sha256 == sha256
                    and item.object_type == "network_capture"
                    and item.object_id == sha256
                    and (
                        item.source == "network.quarantine"
                        or (
                            item.source == "VirusTotal"
                            and item.source_version == _VT_SANDBOX_PCAP_SOURCE_VERSION
                        )
                    )
                ),
                None,
            )
            if observation is None:
                raise NetworkCaptureRejected(NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED)
            blob = await uow.blobs.get(observation.blob_id)

        if blob is None or blob.descriptor.logical_bucket != _NETWORK_CAPTURE_BUCKET:
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_UNAVAILABLE)
        descriptor = blob.descriptor
        if descriptor.sha256 != sha256:
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_HASH_MISMATCH)
        if descriptor.size > effective_max:
            raise QuarantineCaptureTooLargeError("Capture exceeds the requested read limit")

        try:
            content = await self._blob_catalog.read(blob.id, max_bytes=effective_max)
        except BlobReadLimitExceededError as exc:
            raise QuarantineCaptureTooLargeError(
                "Capture exceeds the requested read limit"
            ) from exc
        except (BlobStorageUnavailableError, EntityNotFoundError, OSError, ValueError) as exc:
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_UNAVAILABLE) from exc

        if not isinstance(content, bytes):
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_UNAVAILABLE)
        if len(content) > effective_max:
            raise QuarantineCaptureTooLargeError("Capture exceeds the requested read limit")
        if len(content) != descriptor.size or hashlib.sha256(content).hexdigest() != sha256:
            raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_HASH_MISMATCH)
        try:
            read_capture(
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
        return QuarantinedCapture(source_reference, content, sha256)

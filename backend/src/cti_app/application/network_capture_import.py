"""Bounded operator import for inert PCAP/PCAPNG evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.network_workbench import (
    NetworkCaptureRejected,
    ProductionNetworkCaptureBlobStore,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleOrigin, SampleState
from cti_app.domain.investigation import EvidenceKind, SampleAssociation
from cti_app.domain.investigation import SampleOrigin as AssociationSampleOrigin
from cti_app.domain.network_protocol import (
    NetworkCaptureFormat,
    NetworkCaptureRejection,
    NetworkExtractionLimitsV1,
)
from cti_app.infrastructure.pcap_reader import PcapReaderError, read_capture

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMPORT_SOURCE_VERSION = "network-capture-import.v1"
_OPERATOR_IMPORT_ASSOCIATION_NAMESPACE = UUID("31dd6fa3-e4c0-457e-bf6a-7558e319acc2")
_NETWORK_CAPTURE_QUARANTINE_BUCKET = "network-captures-quarantine"
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
    sample_id: UUID
    association_id: UUID


@dataclass(frozen=True, slots=True)
class NetworkCaptureInventoryItem:
    observation_id: UUID
    source_reference: str
    blob_id: UUID
    sha256: str
    size_bytes: int
    format: NetworkCaptureFormat
    mime_type: str
    observed_at: datetime


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

    async def list_captures(
        self, *, investigation_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[NetworkCaptureInventoryItem, ...]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("network capture page is outside its supported bounds")
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise CaptureInvestigationNotFoundError(str(investigation_id))
            observations = await uow.evidence_observations.list_for_investigation(investigation_id)
            candidates = sorted(
                (
                    item
                    for item in observations
                    if item.source == "network.quarantine"
                    and item.source_version == _IMPORT_SOURCE_VERSION
                    and item.object_type == "network_capture"
                    and item.object_id == item.sha256
                    and _SHA256.fullmatch(item.sha256) is not None
                ),
                key=lambda item: (item.observed_at, str(item.id)),
                reverse=True,
            )
            page = candidates[offset : offset + limit]
            result: list[NetworkCaptureInventoryItem] = []
            formats_by_mime = {mime: capture_format for capture_format, mime in _PCAP_MIME.items()}
            for observation in page:
                blob = await uow.blobs.get(observation.blob_id)
                if (
                    blob is None
                    or blob.descriptor.sha256 != observation.sha256
                    or blob.descriptor.logical_bucket != _NETWORK_CAPTURE_QUARANTINE_BUCKET
                ):
                    continue
                mime_type = blob.descriptor.mime_type.split(";", 1)[0].strip().lower()
                capture_format = formats_by_mime.get(mime_type)
                if capture_format is None:
                    continue
                result.append(
                    NetworkCaptureInventoryItem(
                        observation_id=observation.id,
                        source_reference=f"sha256:{observation.sha256}",
                        blob_id=observation.blob_id,
                        sha256=observation.sha256,
                        size_bytes=blob.descriptor.size,
                        format=capture_format,
                        mime_type=mime_type,
                        observed_at=observation.observed_at,
                    )
                )
        return tuple(result)

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
            investigation = await uow.analyst_investigations.get(investigation_id)
            assert investigation is not None
            subject = await uow.subjects.get(investigation.subject_id)
            if subject is None:
                raise CaptureInvestigationNotFoundError(str(investigation_id))

        artifact_store = ProductionArtifactStore(self._blob_catalog)
        capture_blobs = ProductionNetworkCaptureBlobStore(artifact_store)
        blob_id = await capture_blobs.put_capture(
            content,
            mime_type=_PCAP_MIME[parsed.format],
        )

        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise CaptureInvestigationNotFoundError(str(investigation_id))
            subject = await uow.subjects.get(investigation.subject_id)
            if subject is None:
                raise CaptureInvestigationNotFoundError(str(investigation_id))
            blob = await uow.blobs.get(blob_id)
            if (
                blob is None
                or blob.descriptor.sha256 != actual_sha256
                or blob.descriptor.logical_bucket != _NETWORK_CAPTURE_QUARANTINE_BUCKET
            ):
                raise NetworkCaptureRejected(NetworkCaptureRejection.BLOB_HASH_MISMATCH)

            sample = await uow.samples.get_by_subject_and_blob(subject.id, blob_id)
            if sample is None:
                sample = Sample(
                    subject_id=subject.id,
                    blob_id=blob_id,
                    original_name=actual_sha256,
                    origin="operator_import",
                    acquired_at=datetime.now(UTC),
                    license_restriction=None,
                    tlp=subject.tlp,
                    do_not_submit=True,
                    external_llm_allowed=False,
                    origin_kind=SampleOrigin.MANUAL,
                    state=SampleState.QUARANTINED,
                    source_service="operator_import",
                    source_object_id=actual_sha256,
                    expected_hash=actual_sha256,
                )
                await uow.samples.add(sample)
            elif (
                sample.state not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
                or sample.origin_kind is not SampleOrigin.MANUAL
                or sample.source_service != "operator_import"
            ):
                raise NetworkCaptureRejected(NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED)

            association = next(
                (
                    item
                    for item in await uow.sample_associations.list_for_investigation(
                        investigation_id
                    )
                    if item.sample_sha256 == actual_sha256
                ),
                None,
            )
            if association is None:
                association = SampleAssociation(
                    id=uuid5(
                        _OPERATOR_IMPORT_ASSOCIATION_NAMESPACE,
                        f"{investigation_id}:{actual_sha256}",
                    ),
                    investigation_id=investigation_id,
                    sample_sha256=actual_sha256,
                    origin=AssociationSampleOrigin.MANUAL,
                )
                association = await uow.sample_associations.append_version(
                    association, expected_version=0
                )

            existing_events = await uow.provenance.list_for_aggregate("sample", sample.id)
            if not any(
                event.event_type == "sample_imported_by_operator"
                and event.payload.get("investigation_id") == str(investigation_id)
                and event.payload.get("import_source") == "network_capture"
                and event.payload.get("import_object_id") == actual_sha256
                for event in existing_events
            ):
                await uow.provenance.append(
                    ProvenanceEvent(
                        aggregate_type="sample",
                        aggregate_id=sample.id,
                        subject_id=subject.id,
                        event_type="sample_imported_by_operator",
                        payload={
                            "provenance": "operator_import",
                            "import_source": "network_capture",
                            "investigation_id": str(investigation_id),
                            "import_object_id": actual_sha256,
                            "object_type": "network_capture",
                            "operator_id": operator_id,
                        },
                        tlp=sample.tlp,
                        actor_id=operator_id,
                    )
                )
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
            sample_id=sample.id,
            association_id=association.id,
        )


def capture_max_bytes(settings: Settings) -> int:
    """Honor a runtime override while respecting the network-workbench hard cap."""
    hard_limit = NetworkExtractionLimitsV1().max_capture_bytes
    configured = getattr(settings, "network_capture_max_bytes", hard_limit)
    if isinstance(configured, bool) or not isinstance(configured, int) or configured <= 0:
        raise ValueError("network_capture_max_bytes must be a positive integer")
    return min(configured, hard_limit)

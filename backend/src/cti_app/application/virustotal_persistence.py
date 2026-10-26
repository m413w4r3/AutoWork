"""Canonical storage of already obtained successful VirusTotal responses.

The blob is written/catalogued first.  A single PostgreSQL transaction then
creates the immutable observation and, for file reports, its normalized
view.  A failed transaction can therefore leave an unreferenced, safely
deduplicated blob; retrying the same response reuses it and never issues a
VirusTotal request.
"""

import base64
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4, uuid5

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.virustotal import (
    VirusTotalFile,
    VirusTotalFileReport,
    VirusTotalPage,
    VirusTotalSearchResult,
)
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleState
from cti_app.domain.entities import SampleOrigin as EntitySampleOrigin
from cti_app.domain.external_effects import EvidenceReference
from cti_app.domain.investigation import EvidenceKind, SampleAssociation
from cti_app.domain.investigation import SampleOrigin as AssociationSampleOrigin
from cti_app.domain.network_protocol import (
    NetworkCaptureFormat,
    NetworkCaptureRefV1,
    NetworkCaptureRejection,
    NetworkExtractionLimitsV1,
)
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalFileView,
    VirusTotalObjectType,
    VirusTotalObservation,
    VirusTotalOperation,
)
from cti_app.infrastructure.pcap_reader import PcapReaderError, read_capture

VIRUSTOTAL_RAW_BUCKET = "virustotal-raw"
VIRUSTOTAL_JSON_MIME_TYPE = "application/json"
_logger = logging.getLogger(__name__)
_SAFE_LOG_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_CONSTRAINT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_NETWORK_CAPTURE_QUARANTINE_BUCKET = "network-captures-quarantine"
_VT_SANDBOX_PCAP_ASSOCIATION_NAMESPACE = UUID("a40d5c04-6075-45d8-9aef-4bcd9cae1cb0")
_PCAP_MIME_TYPES = {
    NetworkCaptureFormat.PCAP: "application/vnd.tcpdump.pcap",
    NetworkCaptureFormat.PCAPNG: "application/x-pcapng",
}


@dataclass(frozen=True, slots=True)
class StoredBehaviourPcap:
    capture_ref: NetworkCaptureRefV1
    sample_id: UUID
    association_id: UUID
    evidence_id: UUID


@dataclass(frozen=True, slots=True)
class ArchivedVirusTotalResponse:
    body: bytes
    status_code: int
    content_type_class: str
    reference: EvidenceReference
    location_host: str | None = None
    retry_after: float | None = None
    issuer: str = "unknown"
    oversized: bool = False


class VirusTotalObservationService:
    def __init__(self, catalog: BlobCatalogService, uow_factory: UnitOfWorkFactory) -> None:
        self._catalog = catalog
        self._uow_factory = uow_factory

    async def archive_external_response(
        self,
        content: bytes,
        *,
        status_code: int,
        content_type_class: str,
        location_host: str | None,
        retry_after: float | None,
        issuer: str,
        oversized: bool,
    ) -> EvidenceReference:
        """Persist a bounded gated VT response before its claim is completed."""
        if not isinstance(content, bytes) or not 100 <= status_code <= 599:
            raise ValueError("Only complete HTTP VT responses can be archived")
        if content_type_class not in {"json", "html", "binary", "text", "other", "missing"}:
            raise ValueError("Invalid VT content type classification")
        if issuer not in {"unknown", "proxy-likely", "upstream-vt-likely"}:
            raise ValueError("Invalid VT response issuer")
        if location_host is not None and not re.fullmatch(r"[a-zA-Z0-9.:-]{1,255}", location_host):
            raise ValueError("Invalid VT response location host")
        if retry_after is not None and (
            isinstance(retry_after, bool) or not isinstance(retry_after, int | float)
        ):
            raise ValueError("Invalid VT response retry delay")
        metadata = {
            "content_type_class": content_type_class,
            "issuer": issuer,
            "location_host": location_host,
            "oversized": oversized,
            "retry_after": retry_after,
            "status_code": status_code,
        }
        encoded_metadata = (
            base64.urlsafe_b64encode(
                json.dumps(
                    metadata,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("ascii")
            )
            .rstrip(b"=")
            .decode("ascii")
        )
        content_mime_types = {
            "binary": "application/octet-stream",
            "html": "text/html",
            "json": "application/json",
            "missing": "application/octet-stream",
            "other": "application/octet-stream",
            "text": "text/plain",
        }
        blob = await self._catalog.ingest(
            BytesIO(content),
            logical_bucket=VIRUSTOTAL_RAW_BUCKET,
            mime_type=content_mime_types[content_type_class],
        )
        return EvidenceReference(
            kind="vt.raw_response",
            id=str(blob.id),
            version=f"vt-http.v1.{encoded_metadata}",
            sha256=blob.descriptor.sha256,
        )

    async def read_external_response(
        self, reference: EvidenceReference, *, max_bytes: int
    ) -> ArchivedVirusTotalResponse:
        """Resolve a claim's raw response reference and verify its canonical blob."""
        if reference.kind != "vt.raw_response":
            raise ValueError("VT raw response reference has the wrong kind")
        prefix = "vt-http.v1."
        if not reference.version.startswith(prefix):
            raise ValueError("VT raw response reference version is invalid")
        token = reference.version[len(prefix) :]
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError("VT raw response reference version is invalid")
        try:
            metadata = json.loads(
                base64.b64decode(
                    token + "=" * (-len(token) % 4), altchars=b"-_", validate=True
                ).decode("ascii")
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("VT raw response reference version is invalid") from None
        if not isinstance(metadata, dict):
            raise ValueError("VT raw response reference version is invalid")
        status_code = metadata.get("status_code")
        content_type_class = metadata.get("content_type_class")
        location_host = metadata.get("location_host")
        retry_after = metadata.get("retry_after")
        issuer = metadata.get("issuer")
        oversized = metadata.get("oversized")
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
            or content_type_class not in {"json", "html", "binary", "text", "other", "missing"}
            or (location_host is not None and not isinstance(location_host, str))
            or (
                retry_after is not None
                and (isinstance(retry_after, bool) or not isinstance(retry_after, int | float))
            )
            or issuer not in {"unknown", "proxy-likely", "upstream-vt-likely"}
            or not isinstance(oversized, bool)
        ):
            raise ValueError("VT raw response reference metadata is invalid")
        blob_id = UUID(reference.id)
        async with self._uow_factory() as uow:
            blob = await uow.blobs.get(blob_id)
        if (
            blob is None
            or blob.descriptor.logical_bucket != VIRUSTOTAL_RAW_BUCKET
            or blob.descriptor.sha256 != reference.sha256
        ):
            raise ValueError("VT raw response archive reference is missing or changed")
        content = await self._catalog.read(blob_id, max_bytes=max_bytes)
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("VT raw response archive checksum failed")
        return ArchivedVirusTotalResponse(
            body=content,
            status_code=status_code,
            content_type_class=content_type_class,
            reference=reference,
            location_host=location_host,
            retry_after=float(retry_after) if retry_after is not None else None,
            issuer=issuer,
            oversized=oversized,
        )

    async def store_behaviour_pcap(
        self,
        content: bytes,
        *,
        investigation_id: UUID,
        file_hash: str,
        sandbox_id: str,
        expected_sha256: str,
        max_bytes: int,
        observed_at: datetime,
        idempotency_key: str,
    ) -> StoredBehaviourPcap:
        """Quarantine a verified offline sandbox capture with its VT provenance."""
        if not content or max_bytes < 1 or len(content) > max_bytes:
            raise ValueError(NetworkCaptureRejection.SIZE_LIMIT.value)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(NetworkCaptureRejection.BLOB_HASH_MISMATCH.value)
        if not re.fullmatch(r"[0-9a-f]{64}", actual_sha256):
            raise ValueError(NetworkCaptureRejection.BLOB_HASH_MISMATCH.value)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("PCAP observation time must be timezone-aware")
        try:
            parsed = read_capture(
                content,
                max_packets=NetworkExtractionLimitsV1().max_packets,
            )
        except PcapReaderError as exc:
            raise ValueError(NetworkCaptureRejection.INVALID_FORMAT.value) from exc

        source_object_id = f"{file_hash}:{sandbox_id}"
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise ValueError("PCAP investigation does not exist")
            subject = await uow.subjects.get(investigation.subject_id)
            if subject is None:
                raise ValueError("PCAP investigation subject does not exist")

        artifact_store = ProductionArtifactStore(self._catalog)
        blob_id = await artifact_store.put_bytes(
            content,
            bucket=_NETWORK_CAPTURE_QUARANTINE_BUCKET,
            mime_type=_PCAP_MIME_TYPES[parsed.format],
        )

        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise ValueError("PCAP investigation does not exist")
            subject = await uow.subjects.get(investigation.subject_id)
            if subject is None:
                raise ValueError("PCAP investigation subject does not exist")
            blob = await uow.blobs.get(blob_id)
            if (
                blob is None
                or blob.descriptor.sha256 != actual_sha256
                or blob.descriptor.size != len(content)
                or blob.descriptor.logical_bucket != _NETWORK_CAPTURE_QUARANTINE_BUCKET
            ):
                raise ValueError(NetworkCaptureRejection.BLOB_HASH_MISMATCH.value)

            sample = await uow.samples.get_by_subject_and_blob(subject.id, blob_id)
            if sample is None:
                sample = Sample(
                    subject_id=subject.id,
                    blob_id=blob_id,
                    original_name=actual_sha256,
                    origin="vt_sandbox_pcap",
                    acquired_at=datetime.now(UTC),
                    license_restriction=None,
                    tlp=subject.tlp,
                    do_not_submit=True,
                    external_llm_allowed=False,
                    # No dedicated PCAP origin exists yet; the closest typed origin records
                    # that this inert artifact was obtained from a VirusTotal investigation.
                    origin_kind=EntitySampleOrigin.VT_HUNT_HIT,
                    state=SampleState.QUARANTINED,
                    source_service="virustotal",
                    source_object_id=source_object_id,
                    expected_hash=actual_sha256,
                )
                await uow.samples.add(sample)
            elif (
                sample.state not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
                or sample.origin != "vt_sandbox_pcap"
                or sample.origin_kind is not EntitySampleOrigin.VT_HUNT_HIT
                or sample.source_service != "virustotal"
                or sample.source_object_id != source_object_id
            ):
                raise ValueError(NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED.value)

            associations = await uow.sample_associations.list_for_investigation(investigation_id)
            association = next(
                (item for item in associations if item.sample_sha256 == actual_sha256),
                None,
            )
            if association is None:
                association = SampleAssociation(
                    id=uuid5(
                        _VT_SANDBOX_PCAP_ASSOCIATION_NAMESPACE,
                        f"{investigation_id}:{actual_sha256}",
                    ),
                    investigation_id=investigation_id,
                    sample_sha256=actual_sha256,
                    origin=AssociationSampleOrigin.BEHAVIOR,
                )
                association = await uow.sample_associations.append_version(
                    association, expected_version=0
                )
            elif association.origin is not AssociationSampleOrigin.BEHAVIOR:
                raise ValueError(NetworkCaptureRejection.CAPTURE_NOT_ASSOCIATED.value)

            events = await uow.provenance.list_for_aggregate("sample", sample.id)
            if not any(
                event.event_type == "sample_imported_from_vt_sandbox_pcap"
                and event.payload.get("investigation_id") == str(investigation_id)
                and event.payload.get("file_hash") == file_hash
                and event.payload.get("sandbox_id") == sandbox_id
                and event.payload.get("capture_sha256") == actual_sha256
                for event in events
            ):
                await uow.provenance.append(
                    ProvenanceEvent(
                        aggregate_type="sample",
                        aggregate_id=sample.id,
                        subject_id=subject.id,
                        event_type="sample_imported_from_vt_sandbox_pcap",
                        payload={
                            "provenance": "vt_sandbox_pcap",
                            "investigation_id": str(investigation_id),
                            "file_hash": file_hash,
                            "sandbox_id": sandbox_id,
                            "capture_sha256": actual_sha256,
                            "source_service": "virustotal",
                        },
                        tlp=sample.tlp,
                    )
                )

            recorder = ProductionEvidenceRecorder(
                artifact_store=artifact_store,
                observations=uow.evidence_observations,
            )
            evidence = await recorder.record_existing_blob(
                investigation_id=investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=blob_id,
                sha256=actual_sha256,
                source="VirusTotal",
                source_version="vt-sandbox-pcap.v1",
                object_type="network_capture",
                object_id=actual_sha256,
                limitations=(
                    "Le PCAP/PCAPNG est une capture hors ligne fournie par un sandbox VirusTotal.",
                    "Cette capture n'est pas une observation d'un C2 actif.",
                    "Le contenu est non fiable et n'est jamais exécuté.",
                ),
                observed_at=observed_at,
                idempotency_key=idempotency_key,
            )
            await uow.commit()

        return StoredBehaviourPcap(
            capture_ref=NetworkCaptureRefV1(
                blob_id=blob_id,
                sha256=actual_sha256,
                size_bytes=len(content),
                format=parsed.format,
            ),
            sample_id=sample.id,
            association_id=association.id,
            evidence_id=evidence.id,
        )

    async def store_file_report(
        self,
        report: VirusTotalFileReport,
        *,
        subject_id: UUID | None = None,
        observed_at: datetime,
        checkpoint_id: str | None = None,
    ) -> VirusTotalObservation:
        if checkpoint_id is not None:
            async with self._uow_factory() as uow:
                existing = await uow.virustotal_observations.find_file_report_checkpoint(
                    checkpoint_id, report.file.lookup_value
                )
            if existing is not None:
                return existing
        blob = await self._catalog.ingest(
            BytesIO(report.raw_json),
            logical_bucket=VIRUSTOTAL_RAW_BUCKET,
            mime_type=VIRUSTOTAL_JSON_MIME_TYPE,
        )
        safe_parameters = {
            "file_hash": report.file.lookup_value,
            "transport": report.transport.value,
            "api_generation": report.api_generation.value,
        }
        if checkpoint_id is not None:
            safe_parameters["checkpoint_id"] = checkpoint_id
        observation = VirusTotalObservation(
            operation=VirusTotalOperation.FILE_REPORT,
            capability=VirusTotalCapability.FILE_REPORT,
            source_identifier=report.file.lookup_value,
            safe_parameters=safe_parameters,
            http_status=report.http_status,
            blob_id=blob.id,
            raw_sha256=blob.descriptor.sha256,
            raw_size=blob.descriptor.size,
            observed_at=observed_at,
            subject_id=subject_id,
            input_cursor=None,
            output_cursor=None,
            observed_count=1,
            exhaustive=True,
            page_order=0,
            execution_id=uuid4(),
        )
        view = _file_view(observation.id, report.file)
        await self._persist_observation(observation, raw_blob_id=blob.id, file_view=view)
        return observation

    async def store_file_relationship(
        self,
        page: VirusTotalPage,
        *,
        file_hash: str,
        relation: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        return await self._store_pages(
            page.raw_json_pages,
            page.http_statuses,
            VirusTotalOperation.FILE_RELATIONSHIP,
            VirusTotalCapability.FILE_RELATIONSHIPS,
            file_hash,
            {
                "file_hash": file_hash,
                "relation": relation,
                "limit": page.limit_used,
                "input_cursor": input_cursor,
                "transport": page.transport.value,
                "api_generation": page.api_generation.value,
            },
            page,
            subject_id,
            input_cursor,
            observed_at,
        )

    async def store_object_relationship(
        self,
        page: VirusTotalPage,
        *,
        object_type: VirusTotalObjectType,
        object_id: str,
        relation: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        return await self._store_pages(
            page.raw_json_pages,
            page.http_statuses,
            VirusTotalOperation.OBJECT_RELATIONSHIP,
            VirusTotalCapability.FILE_RELATIONSHIPS,
            object_id,
            {
                "object_type": object_type.value,
                "object_id": object_id,
                "relation": relation,
                "limit": page.limit_used,
                "input_cursor": input_cursor,
                "transport": page.transport.value,
                "api_generation": page.api_generation.value,
            },
            page,
            subject_id,
            input_cursor,
            observed_at,
        )

    async def store_behaviours(
        self,
        page: VirusTotalPage,
        *,
        file_hash: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        return await self._store_pages(
            page.raw_json_pages,
            page.http_statuses,
            VirusTotalOperation.BEHAVIOURS,
            VirusTotalCapability.BEHAVIOUR_REPORTS,
            file_hash,
            {
                "file_hash": file_hash,
                "limit": page.limit_used,
                "input_cursor": input_cursor,
                "transport": page.transport.value,
                "api_generation": page.api_generation.value,
            },
            page,
            subject_id,
            input_cursor,
            observed_at,
        )

    async def store_intelligence_search(
        self,
        result: VirusTotalSearchResult,
        *,
        query: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
        descriptors_only: bool | None = None,
        order: str | None = None,
    ) -> tuple[VirusTotalObservation, ...]:
        effective_descriptors_only = (
            result.descriptors_only if descriptors_only is None else descriptors_only
        )
        effective_order = result.order if order is None else order
        return await self._store_pages(
            result.raw_json_pages,
            result.http_statuses,
            VirusTotalOperation.INTELLIGENCE_SEARCH,
            VirusTotalCapability.INTELLIGENCE_SEARCH,
            query,
            {
                "query": query,
                "limit": result.limit_used,
                "input_cursor": input_cursor,
                "descriptors_only": effective_descriptors_only,
                "order": effective_order,
                "transport": result.transport.value,
                "api_generation": result.api_generation.value,
            },
            result,
            subject_id,
            input_cursor,
            observed_at,
        )

    async def _store_pages(
        self,
        bodies: tuple[bytes, ...],
        http_statuses: tuple[int, ...],
        operation: VirusTotalOperation,
        capability: VirusTotalCapability,
        source: str,
        parameters: dict[str, Any],
        result: VirusTotalPage | VirusTotalSearchResult,
        subject_id: UUID | None,
        input_cursor: str | None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        execution_id = uuid4()
        observations: list[VirusTotalObservation] = []
        cursor = input_cursor
        for order, (body, http_status) in enumerate(zip(bodies, http_statuses, strict=True)):
            payload = json.loads(body)
            data = payload.get("data", [])
            emitted = _cursor(payload)
            blob = await self._catalog.ingest(
                BytesIO(body),
                logical_bucket=VIRUSTOTAL_RAW_BUCKET,
                mime_type=VIRUSTOTAL_JSON_MIME_TYPE,
            )
            observation = VirusTotalObservation(
                operation=operation,
                capability=capability,
                source_identifier=source,
                safe_parameters=parameters,
                http_status=http_status,
                blob_id=blob.id,
                raw_sha256=blob.descriptor.sha256,
                raw_size=blob.descriptor.size,
                observed_at=observed_at,
                subject_id=subject_id,
                input_cursor=cursor,
                output_cursor=emitted,
                observed_count=len(data) if isinstance(data, list) else 0,
                exhaustive=result.exhaustive if order == len(bodies) - 1 else False,
                page_order=order,
                execution_id=execution_id,
            )
            await self._persist_observation(observation, raw_blob_id=blob.id)
            observations.append(observation)
            cursor = emitted
        return tuple(observations)

    async def _persist_observation(
        self,
        observation: VirusTotalObservation,
        *,
        raw_blob_id: UUID,
        file_view: VirusTotalFileView | None = None,
    ) -> None:
        try:
            async with self._uow_factory() as uow:
                await uow.virustotal_observations.add(observation)
                if file_view is not None:
                    await uow.virustotal_file_views.add_if_absent(file_view)
                await uow.commit()
        except Exception as exc:
            _logger.warning(
                "VirusTotal observation persistence failed after raw response archive",
                extra={
                    "event": "vt_observation_persistence_failed",
                    "operation": observation.operation.value,
                    "capability": observation.capability.value,
                    "exception_class": _safe_exception_class(exc),
                    "constraint_name": _constraint_name(exc),
                    "observation_id": str(observation.id),
                    "raw_blob_id": str(raw_blob_id),
                    "raw_response_archived": True,
                },
            )
            raise


def _cursor(payload: dict[str, Any]) -> str | None:
    meta = payload.get("meta")
    value = meta.get("cursor") if isinstance(meta, dict) else None
    return value if isinstance(value, str) else None


def _safe_exception_class(exc: Exception) -> str:
    name = type(exc).__name__
    return name if _SAFE_LOG_IDENTIFIER.fullmatch(name) else "UnknownException"


def _constraint_name(exc: Exception) -> str:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    for _ in range(12):
        if not pending:
            break
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        diagnostic = getattr(current, "diag", None)
        candidates = (
            getattr(current, "constraint_name", None),
            getattr(diagnostic, "constraint_name", None),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and _SAFE_CONSTRAINT_NAME.fullmatch(candidate):
                return candidate
        for nested in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(nested, BaseException) and id(nested) not in seen:
                pending.append(nested)
    return "unknown"


def _file_view(observation_id: UUID, file: VirusTotalFile) -> VirusTotalFileView:
    return VirusTotalFileView(
        observation_id=observation_id,
        vt_file_id=file.id,
        file_type=file.type,
        lookup_hash=file.lookup_value,
        meaningful_name=file.meaningful_name,
        type_description=file.type_description,
        size=file.size,
        last_analysis_stats=file.last_analysis_stats,
        first_submission_date=file.first_submission_date,
        last_submission_date=file.last_submission_date,
        last_modification_date=file.last_modification_date,
        tags=file.tags,
        vhash=file.vhash,
        imphash=file.imphash,
        ssdeep=file.ssdeep,
        tlsh=file.tlsh,
        main_icon_dhash=file.main_icon_dhash,
        rich_header_hash=file.rich_header_hash,
    )

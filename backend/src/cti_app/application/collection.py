from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

from pydantic import ConfigDict, Field

from cti_app.application.acquisition_fallbacks import (
    AcquisitionProbe,
    AcquisitionRequest,
    AcquisitionStrategy,
    _same_acquisition_publisher,
    _typed_probe_outcome,
    article_rejection_reason,
    bridge_article_response_rejection,
    bridge_location_metadata_rejection,
    bridge_source_discovery_prompt,
    bridge_transcription_prompt,
    bridge_url_is_credential_free,
    default_acquisition_strategies,
    deterministic_acquisition_quality,
    parse_bridge_source_locations,
    parse_bridge_transcription,
    safe_fallback_url,
)
from cti_app.application.acquisition_orchestrator import (
    ArticleAcquisitionOrchestrator,
    DomainConcurrencyLimiter,
)
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.collection_errors import (
    CollectionItemNotFoundError,
    CollectionNotAllowedError,
)
from cti_app.application.extraction import DocumentParsingError, parse_document
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionError,
    DownloadTooLargeError,
    SafeHttpCollector,
    UnsafeAddressError,
    UnsupportedContentError,
    _detect_mime,
    parse_domain_policy,
)
from cti_app.application.jobs import (
    JobCancelledError,
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.model_gateway import (
    ModelExecution,
    ModelGateway,
    ModelRequest,
    ModelRoutingHint,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.source_filenames import analyst_filename
from cti_app.application.source_media_collection import SourceMediaArchiveService
from cti_app.application.subject_lineage import resolve_subject_discovery_lineage
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.domain.acquisition import (
    AcquisitionCapabilityState,
    AcquisitionObservationKind,
    AcquisitionOutcome,
    AcquisitionPolicyContext,
    AcquisitionPolicyDecision,
    AcquisitionQuality,
    EvidenceClass,
    SourceEquivalenceClass,
    credential_free_url,
    evidence_class_for_equivalence,
    is_credential_free_url,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    AttemptOutcome,
    CollectionAttempt,
    CollectionFailureReason,
    CollectionPolicySnapshot,
    CollectionResolutionProvenance,
    CollectionState,
    DetectedMimeType,
    SourceAcquisitionMethod,
    SourceCollection,
    SourceOriginKind,
)
from cti_app.domain.discovery import (
    DiscoveryCandidate,
    SourceCandidate,
    SourceRole,
    canonicalize_http_url,
)
from cti_app.domain.entities import ProvenanceEvent, SourceDocument
from cti_app.domain.production import ProductionInputSnapshot, ProductionInputSource
from cti_app.logging import get_correlation_id

_FALLBACK_MODEL_RUN_NAMESPACE = UUID("79af24d6-1b5b-42b8-b21d-5116e2c3b2f5")

logger = logging.getLogger(__name__)
_COLLECTED_STATES = {
    CollectionState.ARCHIVED,
    CollectionState.EXTRACTED,
    CollectionState.COMPLETED,
}
_CREDENTIAL_LIKE_URL_REJECTION = "credential-like URL rejected"


def _snapshot_source_urls(snapshot: ProductionInputSnapshot) -> frozenset[str]:
    """Return the frozen source URL set in deterministic order-independent form."""
    return frozenset(
        _safe_collection_url(source.canonical_url)[0] for source in snapshot.core_sources
    )


class ManualContentTypeError(CollectionNotAllowedError):
    """The supplied bytes do not contain a supported source document."""


class ManualContentEmptyError(CollectionNotAllowedError):
    """The supplied source document has no content."""


class ManualContentTooLargeError(CollectionNotAllowedError):
    """The supplied source document exceeds the collection policy limit."""


class ManualContentAlreadyArchivedError(CollectionNotAllowedError):
    """The immutable archived source cannot be replaced."""


@dataclass(frozen=True, slots=True)
class _CollectorAcquisition:
    """The collector downloaded the publication under a real job lease."""

    job_id: UUID


@dataclass(frozen=True, slots=True)
class _ManualAcquisition:
    """The analyst supplied the publication under a manual upload lease."""

    manual_lease_id: UUID
    actor_id: str
    declared_mime_type: str


@dataclass(frozen=True, slots=True)
class _FallbackAcquisition:
    """Content obtained via a same-publication variant, archive, or Bridge."""

    job_id: UUID
    method: SourceAcquisitionMethod
    model_run_id: UUID | None = None
    consultation_id: str | None = None
    prompt_sha256: str | None = None
    response_sha256: str | None = None
    strategy_name: str = "bridge_transcription"
    strategy_version: str = "1"
    duration_ms: int = 0
    strategy_url: str | None = None
    source_equivalence_class: SourceEquivalenceClass | None = None
    evidence_class: EvidenceClass | None = None
    observation_kind: AcquisitionObservationKind | None = None
    quality: AcquisitionQuality | None = None
    archive_snapshot_time: datetime | None = None
    publication_date: date | None = None
    publisher: str | None = None
    article_title: str | None = None
    model_generated: bool = False
    primary_source_verified: bool = False
    ioc_primary_verified: bool = False


@dataclass(frozen=True, slots=True)
class ManualArchiveReceipt:
    """The immutable facts of a successful analyst-supplied archive."""

    subject_id: UUID
    collection_id: UUID
    source_document_id: UUID
    raw_blob_id: UUID
    decoded_blob_id: UUID
    encoded_sha256: str
    decoded_sha256: str
    bytes: int
    declared_mime_type: str
    detected_mime_type: str
    actor_id: str
    correlation_id: str
    completed_at: datetime

    def log_fields(self) -> dict[str, object]:
        """Return only metadata; raw analyst content is never loggable here."""
        return {
            "subject_id": str(self.subject_id),
            "collection_id": str(self.collection_id),
            "source_document_id": str(self.source_document_id),
            "raw_blob_id": str(self.raw_blob_id),
            "decoded_blob_id": str(self.decoded_blob_id),
            "encoded_sha256": self.encoded_sha256,
            "decoded_sha256": self.decoded_sha256,
            "bytes": self.bytes,
            "declared_mime": self.declared_mime_type,
            "detected_mime": self.detected_mime_type,
            "declared_mime_type": self.declared_mime_type,
            "detected_mime_type": self.detected_mime_type,
            "actor_id": self.actor_id,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_provenance(cls, event: ProvenanceEvent) -> ManualArchiveReceipt | None:
        """Rebuild the receipt from the durable ``source.archived_manually`` row.

        The receipt is never stored as such: the provenance event already
        carries every fact, so a reload reconstructs it instead of trusting a
        response the browser happened to keep.
        """
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.subject_id is None:
            return None
        try:
            return cls(
                subject_id=event.subject_id,
                collection_id=event.aggregate_id,
                source_document_id=UUID(str(payload["source_document_id"])),
                raw_blob_id=UUID(str(payload["raw_blob_id"])),
                decoded_blob_id=UUID(str(payload["decoded_blob_id"])),
                encoded_sha256=str(payload["encoded_sha256"]),
                decoded_sha256=str(payload["decoded_sha256"]),
                bytes=int(payload["size"]),
                declared_mime_type=str(payload["declared_mime_type"]),
                detected_mime_type=str(payload["detected_mime_type"]),
                actor_id=str(event.actor_id or payload.get("actor_id") or ""),
                correlation_id=str(payload.get("correlation_id") or ""),
                completed_at=event.occurred_at,
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class SourceArchiveReceipt:
    """Queryable immutable archive facts, including the acquisition method."""

    subject_id: UUID
    collection_id: UUID
    source_document_id: UUID
    raw_blob_id: UUID
    decoded_blob_id: UUID
    encoded_sha256: str
    decoded_sha256: str
    bytes: int
    declared_mime_type: str
    detected_mime_type: str
    acquisition_method: SourceAcquisitionMethod
    actor_id: str
    correlation_id: str
    completed_at: datetime
    model_run_id: UUID | None = None
    consultation_id: str | None = None
    prompt_sha256: str | None = None
    response_sha256: str | None = None
    verification_status: str | None = None
    strategy_name: str | None = None
    strategy_version: str | None = None
    duration_ms: int | None = None
    http_status: int | None = None
    strategy_bytes: int | None = None
    strategy_sha256: str | None = None
    source_equivalence_class: SourceEquivalenceClass | None = None
    evidence_class: EvidenceClass | None = None
    observation_kind: AcquisitionObservationKind | None = None
    quality_score: float | None = None
    quality_reasons: tuple[str, ...] = ()
    archive_snapshot_time: datetime | None = None
    model_generated: bool | None = None
    primary_source_verified: bool | None = None
    ioc_primary_verified: bool | None = None

    @classmethod
    def from_provenance(cls, event: ProvenanceEvent) -> SourceArchiveReceipt | None:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.subject_id is None or event.event_type not in {
            "source.archived",
            "source.archived_fallback",
            "source.archived_manually",
        }:
            return None
        try:
            method = SourceAcquisitionMethod(
                str(
                    payload.get("acquisition_method")
                    or (
                        SourceAcquisitionMethod.MANUAL_UPLOAD.value
                        if event.event_type == "source.archived_manually"
                        else SourceAcquisitionMethod.DIRECT_HTTP.value
                    )
                )
            )
            model_run = payload.get("model_run_id")
            quality = payload.get("quality_assessment")
            quality = quality if isinstance(quality, dict) else {}
            archive_snapshot_time = payload.get("archive_snapshot_time")
            size_value = payload.get("size", payload.get("decoded_size", 0))
            return cls(
                subject_id=event.subject_id,
                collection_id=event.aggregate_id,
                source_document_id=UUID(str(payload["source_document_id"])),
                raw_blob_id=UUID(str(payload["raw_blob_id"])),
                decoded_blob_id=UUID(str(payload["decoded_blob_id"])),
                encoded_sha256=str(payload["encoded_sha256"]),
                decoded_sha256=str(payload["decoded_sha256"]),
                bytes=int(size_value) if size_value is not None else 0,
                declared_mime_type=str(
                    payload.get("declared_mime_type") or "application/octet-stream"
                ),
                detected_mime_type=str(payload["detected_mime_type"]),
                acquisition_method=method,
                actor_id=str(event.actor_id or payload.get("actor_id") or ""),
                correlation_id=str(payload.get("correlation_id") or ""),
                completed_at=event.occurred_at,
                model_run_id=UUID(str(model_run)) if model_run else None,
                consultation_id=(
                    str(payload["consultation_id"]) if payload.get("consultation_id") else None
                ),
                prompt_sha256=(
                    str(payload["prompt_sha256"]) if payload.get("prompt_sha256") else None
                ),
                response_sha256=(
                    str(payload["response_sha256"]) if payload.get("response_sha256") else None
                ),
                verification_status=(
                    str(payload["verification_status"])
                    if payload.get("verification_status")
                    else None
                ),
                strategy_name=(
                    str(payload["strategy_name"]) if payload.get("strategy_name") else None
                ),
                strategy_version=(
                    str(payload["strategy_version"]) if payload.get("strategy_version") else None
                ),
                duration_ms=(
                    int(payload["duration_ms"]) if payload.get("duration_ms") is not None else None
                ),
                http_status=(
                    int(payload["http_status"]) if payload.get("http_status") is not None else None
                ),
                strategy_bytes=(
                    int(payload["bytes"]) if payload.get("bytes") is not None else None
                ),
                strategy_sha256=(str(payload["sha256"]) if payload.get("sha256") else None),
                source_equivalence_class=(
                    SourceEquivalenceClass(str(payload["source_equivalence_class"]))
                    if payload.get("source_equivalence_class")
                    else None
                ),
                evidence_class=(
                    EvidenceClass(str(payload["evidence_class"]))
                    if payload.get("evidence_class")
                    else None
                ),
                observation_kind=(
                    AcquisitionObservationKind(str(payload["observation_kind"]))
                    if payload.get("observation_kind")
                    else None
                ),
                quality_score=(
                    float(quality["score"]) if quality.get("score") is not None else None
                ),
                quality_reasons=tuple(
                    item for item in quality.get("reasons", ()) if isinstance(item, str)
                ),
                archive_snapshot_time=(
                    datetime.fromisoformat(str(archive_snapshot_time))
                    if archive_snapshot_time
                    else None
                ),
                model_generated=(
                    bool(payload["model_generated"])
                    if payload.get("model_generated") is not None
                    else None
                ),
                primary_source_verified=(
                    bool(payload["primary_source_verified"])
                    if payload.get("primary_source_verified") is not None
                    else None
                ),
                ioc_primary_verified=(
                    bool(payload["ioc_primary_verified"])
                    if payload.get("ioc_primary_verified") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class SupplementalSource:
    """A publication proposed by reference research, not by discovery."""

    url: str
    title: str | None = None
    publisher: str | None = None
    published_at: date | None = None
    role: SourceRole = SourceRole.UNKNOWN


@dataclass(frozen=True, slots=True)
class ReferencedEvidence:
    """A deterministic technical resource linked by an archived publication."""

    parent_source_collection_id: UUID
    url: str
    anchor_text: str = ""


@dataclass(slots=True)
class CollectionSummary:
    total: int = 0
    already_archived: int = 0
    newly_archived: int = 0
    unavailable: int = 0
    blocked: int = 0
    failed_retryable: int = 0
    failed_terminal: int = 0

    @property
    def success_count(self) -> int:
        return self.already_archived + self.newly_archived

    @property
    def warning_count(self) -> int:
        return self.unavailable + self.blocked + self.failed_retryable + self.failed_terminal

    def record(self, state: CollectionState, *, already_archived: bool = False) -> None:
        if state in _COLLECTED_STATES:
            if already_archived:
                self.already_archived += 1
            else:
                self.newly_archived += 1
        elif state is CollectionState.UNAVAILABLE:
            self.unavailable += 1
        elif state is CollectionState.BLOCKED:
            self.blocked += 1
        elif state in {CollectionState.FAILED_RETRYABLE, CollectionState.FETCHING}:
            self.failed_retryable += 1
        elif state is CollectionState.FAILED_TERMINAL:
            self.failed_terminal += 1


class SubjectCollectionParameters(JobParameters):
    model_config = ConfigDict(extra="forbid", strict=False)

    subject_id: UUID
    collection_id: UUID | None = None
    requested_by: str = Field(min_length=1, max_length=255)


class FallbackAcquireParameters(JobParameters):
    model_config = ConfigDict(extra="forbid", strict=False)

    subject_id: UUID
    collection_id: UUID
    requested_by: str = Field(min_length=1, max_length=255)


class SubjectCollectionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        collector: SafeHttpCollector,
        blob_store: BlobStore,
        *,
        fetch_lease_seconds: float = 120.0,
        workspace_materializer: SubjectWorkspaceMaterializer | None = None,
        workspace_root: Path | None = None,
        model_gateway: ModelGateway | None = None,
        fallback_variants_enabled: bool = True,
        fallback_archive_enabled: bool = True,
        fallback_bridge_enabled: bool = True,
        fallback_max_variants: int = 4,
        fallback_strategy_flags: dict[str, bool] | None = None,
        fallback_strategy_timeouts: dict[str, float] | None = None,
        fallback_strategies: Sequence[AcquisitionStrategy] | None = None,
        fallback_min_text_chars: int = 200,
        fallback_playwright_sidecar_url: str | None = None,
        acquisition_deadline_seconds: float = 120.0,
        acquisition_max_attempts: int = 40,
        acquisition_per_domain_concurrency: int = 2,
        fallback_endpoint_enabled: bool = True,
        browser_navigation_enabled: bool = False,
        browser_control_socket: str = "",
        syndication_domains: str = "",
        exact_title_search_endpoint: str = "",
        third_party_provider_url: str = "",
        third_party_provider_token: str = "",
        third_party_provider_enabled: bool = False,
        ocr_command: str = "",
        newsletter_archive_endpoint: str = "",
    ) -> None:
        self._uow_factory = uow_factory
        self._collector = collector
        self._blob_store = blob_store
        self._catalog = BlobCatalogService(blob_store, uow_factory)
        self._source_media_archiver = SourceMediaArchiveService(
            uow_factory,
            collector,
            blob_store,
        )
        self._policy = self._collector.policy
        self._workspace_materializer = workspace_materializer
        self._workspace_root = workspace_root
        self._model_gateway = model_gateway
        self._fallback_variants_enabled = fallback_variants_enabled
        self._fallback_archive_enabled = fallback_archive_enabled
        self._fallback_bridge_enabled = fallback_bridge_enabled
        self._fallback_endpoint_enabled = fallback_endpoint_enabled
        self._browser_navigation_enabled = browser_navigation_enabled
        self._browser_control_socket = browser_control_socket
        self._allowed_syndication_domains = parse_domain_policy(syndication_domains)
        self._exact_title_search_endpoint = exact_title_search_endpoint
        self._third_party_provider_url = third_party_provider_url
        self._third_party_provider_token = third_party_provider_token
        self._third_party_provider_enabled = third_party_provider_enabled
        self._ocr_command = ocr_command
        self._newsletter_archive_endpoint = newsletter_archive_endpoint
        if acquisition_deadline_seconds <= 0:
            raise ValueError("acquisition_deadline_seconds must be positive")
        if acquisition_max_attempts < 1:
            raise ValueError("acquisition_max_attempts must be positive")
        if acquisition_per_domain_concurrency < 1:
            raise ValueError("acquisition_per_domain_concurrency must be positive")
        self._acquisition_deadline_seconds = acquisition_deadline_seconds
        self._acquisition_max_attempts = acquisition_max_attempts
        self._acquisition_per_domain_concurrency = acquisition_per_domain_concurrency
        self._acquisition_domain_limiter = DomainConcurrencyLimiter(
            acquisition_per_domain_concurrency
        )
        if fallback_max_variants < 1:
            raise ValueError("fallback_max_variants must be positive")
        self._fallback_max_variants = fallback_max_variants
        if fallback_min_text_chars < 50:
            raise ValueError("fallback_min_text_chars must be at least 50")
        self._fallback_min_text_chars = fallback_min_text_chars
        self._fallback_acquisition_enabled = (
            fallback_strategy_flags is not None or fallback_strategies is not None
        )
        strategy_flags = dict(fallback_strategy_flags or {})
        if fallback_strategy_flags is None and fallback_strategies is None:
            # Runtime composition passes the Settings registry explicitly. A
            # service constructed in isolation remains deterministic/offline.
            strategy_flags = {
                name: False
                for name in (
                    "http_plain",
                    "http_standard_headers",
                    "http_same_site_referer",
                    "http_retry_after",
                    "http2",
                    "curl",
                    "representations",
                    "rss_atom_jsonfeed",
                    "publisher_public_api",
                    "embedded_structured_data",
                    "playwright_chromium_dom",
                    "playwright_network_response",
                    "playwright_embedded_state",
                    "wayback_snapshot",
                    "archive_ph",
                    "commoncrawl_warc",
                    "memento_archive",
                    "exact_title_search",
                    "publisher_alternative_url",
                    "authorized_syndication",
                    "publisher_pdf",
                    "visual_ocr",
                    "official_newsletter_archive",
                    "third_party_extraction",
                    "bridge_source_discovery",
                    "bridge_transcription",
                )
            }
        strategy_flags["representations"] = (
            strategy_flags.get("representations", True) and fallback_variants_enabled
        )
        for names in (
            ("wayback_snapshot", "wayback"),
            ("archive_ph",),
            ("commoncrawl_warc", "common_crawl"),
            ("memento_archive", "memento"),
        ):
            primary = names[0]
            legacy = names[1] if len(names) > 1 else primary
            strategy_flags[primary] = (
                strategy_flags.get(primary, strategy_flags.get(legacy, True))
                and fallback_archive_enabled
            )
        strategy_flags["bridge_transcription"] = (
            strategy_flags.get("bridge_transcription", True) and fallback_bridge_enabled
        )
        strategy_flags["bridge_source_discovery"] = (
            strategy_flags.get("bridge_source_discovery", True) and fallback_bridge_enabled
        )
        self._fallback_strategies = (
            tuple(fallback_strategies)
            if fallback_strategies is not None
            else tuple(
                default_acquisition_strategies(
                    flags=strategy_flags,
                    timeouts=fallback_strategy_timeouts,
                    max_candidates=fallback_max_variants,
                    domain_limiter=self._acquisition_domain_limiter,
                    browser_service_url=fallback_playwright_sidecar_url,
                    browser_control_socket=browser_control_socket,
                    browser_navigation_enabled=browser_navigation_enabled,
                    exact_title_search_endpoint=exact_title_search_endpoint,
                    syndication_domains=self._allowed_syndication_domains,
                    third_party_provider_url=third_party_provider_url,
                    third_party_provider_token=third_party_provider_token,
                    third_party_provider_enabled=third_party_provider_enabled,
                    newsletter_archive_endpoint=newsletter_archive_endpoint,
                    ocr_command=ocr_command,
                    min_text_chars=fallback_min_text_chars,
                )
            )
        )
        self._fetch_lease = timedelta(
            seconds=max(fetch_lease_seconds, self._collector.policy.timeout_seconds + 5)
        )
        self._policy_snapshot = self._collector.policy.snapshot()

    @property
    def fallback_acquisition_enabled(self) -> bool:
        return self._fallback_acquisition_enabled and self._fallback_endpoint_enabled

    async def read_blob(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        """Read archived content back, for consumers holding only a blob id."""
        return await self._catalog.read(blob_id, max_bytes=max_bytes)

    async def initialize(self, subject_id: UUID) -> list[SourceCollection]:
        async with self._uow_factory() as uow:
            lineage = await resolve_subject_discovery_lineage(uow, subject_id)
            subject = lineage.subject
            # Une nouvelle contribution peut réintroduire une URL déjà rattachée au
            # sujet sous un SourceCandidate.id différent. La clé d'unicité en base
            # étant (subject_id, source_candidate_id), il faut dédupliquer sur
            # l'URL canonique face à ce qui est déjà collecté ou archivé (§28).
            seen_urls: set[str] = {
                _safe_collection_url(canonicalize_http_url(collection.requested_url))[0]
                for collection in await uow.source_collections.list_for_subject(subject_id)
            }
            seen_urls.update(
                _safe_collection_url(canonicalize_http_url(document.origin))[0]
                for document in await uow.source_documents.list_for_subject(subject_id)
                if document.origin
            )
            for candidate in lineage.members:
                for source in candidate.evidence.sources:
                    stored_url, _credential_like = _safe_collection_url(source.canonical_url)
                    if stored_url in seen_urls:
                        continue
                    seen_urls.add(stored_url)
                    await uow.source_collections.add_if_absent(
                        _new_collection(
                            subject.edition_id,
                            subject_id,
                            candidate,
                            source,
                        )
                    )
            await uow.commit()
        return await self.list_sources(subject_id)

    async def initialize_from_snapshot(
        self, subject_id: UUID, snapshot: ProductionInputSnapshot
    ) -> list[SourceCollection]:
        """Initialize production sources from the run's immutable input."""
        if snapshot.subject_id != subject_id:
            raise CollectionNotAllowedError("Production snapshot does not belong to the subject")

        snapshot_urls = _snapshot_source_urls(snapshot)
        async with self._uow_factory() as uow:
            current = list(await uow.source_collections.list_for_subject(subject_id))
            seen_urls = {item.canonical_url for item in current}
            for source in snapshot.core_sources:
                stored_url, _credential_like = _safe_collection_url(source.canonical_url)
                if stored_url in seen_urls:
                    continue
                await uow.source_collections.add_if_absent(
                    _new_snapshot_collection(snapshot, subject_id, source)
                )
                seen_urls.add(stored_url)
            await uow.commit()
            current = list(await uow.source_collections.list_for_subject(subject_id))

        return [item for item in current if item.canonical_url in snapshot_urls]

    async def add_supplemental_sources(
        self,
        subject_id: UUID,
        sources: Sequence[SupplementalSource],
    ) -> list[SourceCollection]:
        """Attach sources found during reference research.

        A URL already attached to the subject is reused as-is and never
        re-downloaded; a genuinely new one is registered as REFERENCE_RESEARCH
        so the normal collection pass picks it up.
        """
        async with self._uow_factory() as uow:
            subject = await uow.subjects.get(subject_id)
            if subject is None:
                raise CollectionNotAllowedError(
                    "Only existing subjects can receive supplemental sources"
                )
            added: list[SourceCollection] = []
            for candidate in sources:
                try:
                    canonical = canonicalize_http_url(candidate.url)
                except ValueError:
                    continue
                stored_url, credential_like = _safe_collection_url(canonical)
                existing = await uow.source_collections.get_by_canonical_url(subject_id, stored_url)
                if existing is not None:
                    continue
                collection = SourceCollection(
                    subject_id=subject_id,
                    edition_id=subject.edition_id,
                    requested_url=stored_url,
                    canonical_url=stored_url,
                    origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
                    proposed_role=candidate.role,
                    title=candidate.title,
                    publisher=candidate.publisher,
                    published_at=candidate.published_at,
                    error_reason=(_CREDENTIAL_LIKE_URL_REJECTION if credential_like else None),
                )
                if await uow.source_collections.add_if_absent(collection):
                    added.append(collection)
            await uow.commit()
        return added

    async def ensure_supplemental_source(
        self, subject_id: UUID, source: SupplementalSource
    ) -> SourceCollection:
        """Return the collection for one Q1 proposal, creating it if needed.

        Q1 can propose a publication the collection pass never registered, which
        leaves the Repair Desk with a source it cannot attach any content to.
        This is the idempotent command that closes that gap: it reuses
        ``add_supplemental_sources`` for the creation, so a prepared source is
        indistinguishable from one the reference research attached itself, and
        it contacts no model and no network.
        """
        try:
            canonical = canonicalize_http_url(source.url)
        except ValueError as exc:
            raise CollectionNotAllowedError("Source URL cannot be canonicalized") from exc

        stored_url, _credential_like = _safe_collection_url(canonical)
        async with self._uow_factory() as uow:
            existing = await uow.source_collections.get_by_canonical_url(subject_id, stored_url)
        if existing is not None:
            return existing

        await self.add_supplemental_sources(subject_id, [replace(source, url=canonical)])

        async with self._uow_factory() as uow:
            created = await uow.source_collections.get_by_canonical_url(subject_id, stored_url)
        if created is None:
            raise CollectionNotAllowedError("The proposed source could not be attached")
        return created

    async def add_referenced_evidence(
        self,
        subject_id: UUID,
        resources: Sequence[ReferencedEvidence],
    ) -> list[SourceCollection]:
        """Attach bounded, first-level technical resources without making S# sources.

        The subject/canonical URL uniqueness constraint makes this safe to run
        repeatedly and also reuses a URL that is already a real publication.
        """
        added: list[SourceCollection] = []
        async with self._uow_factory() as uow:
            all_collections = list(await uow.source_collections.list_for_subject(subject_id))
            by_id = {item.id: item for item in all_collections}
            existing_children = sum(
                item.origin_kind is SourceOriginKind.REFERENCED_EVIDENCE for item in all_collections
            )
            per_parent: dict[UUID, int] = {}
            for item in all_collections:
                if item.parent_source_collection_id is not None:
                    per_parent[item.parent_source_collection_id] = (
                        per_parent.get(item.parent_source_collection_id, 0) + 1
                    )
            for resource in resources:
                if existing_children >= 20:
                    break
                parent = by_id.get(resource.parent_source_collection_id)
                if (
                    parent is None
                    or parent.origin_kind is SourceOriginKind.REFERENCED_EVIDENCE
                    or per_parent.get(parent.id, 0) >= 8
                ):
                    continue
                try:
                    canonical = canonicalize_http_url(resource.url)
                except ValueError:
                    continue
                stored_url, credential_like = _safe_collection_url(canonical)
                if await uow.source_collections.get_by_canonical_url(subject_id, stored_url):
                    continue
                child = SourceCollection(
                    subject_id=parent.subject_id,
                    edition_id=parent.edition_id,
                    requested_url=stored_url,
                    canonical_url=stored_url,
                    origin_kind=SourceOriginKind.REFERENCED_EVIDENCE,
                    parent_source_collection_id=parent.id,
                    proposed_role=SourceRole.UNKNOWN,
                    title=resource.anchor_text or None,
                    source_tlp=parent.source_tlp,
                    sensitivity=parent.sensitivity,
                    external_llm_allowed=parent.external_llm_allowed,
                    do_not_submit=parent.do_not_submit,
                    relationship_evidence="deterministic:referenced_evidence_link",
                    error_reason=(_CREDENTIAL_LIKE_URL_REJECTION if credential_like else None),
                )
                if await uow.source_collections.add_if_absent(child):
                    added.append(child)
                    existing_children += 1
                    per_parent[parent.id] = per_parent.get(parent.id, 0) + 1
            await uow.commit()
        return added

    async def list_sources(self, subject_id: UUID) -> list[SourceCollection]:
        async with self._uow_factory() as uow:
            return list(await uow.source_collections.list_for_subject(subject_id))

    async def subject_exists(self, subject_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            return await uow.subjects.get(subject_id) is not None

    @property
    def policy_snapshot(self) -> CollectionPolicySnapshot:
        return self._policy_snapshot

    @property
    def source_media_archiver(self) -> SourceMediaArchiveService:
        return self._source_media_archiver

    async def attempts(self, collection_id: UUID) -> list[CollectionAttempt]:
        async with self._uow_factory() as uow:
            if await uow.source_collections.get(collection_id) is None:
                raise CollectionItemNotFoundError(str(collection_id))
            return list(await uow.collection_attempts.list_for_collection(collection_id))

    async def source_context(
        self, source: SourceCollection
    ) -> tuple[SourceCandidate | None, SourceDocument | None, UUID | None]:
        async with self._uow_factory() as uow:
            candidate, owners = await _discovery_source(
                uow, source.batch_id, source.source_candidate_id
            )
            # Only an unambiguous owner may be handed back as the canonical
            # candidate identity for later candidate-addressed mutations.
            discovery_candidate_id = owners[0].id if len(owners) == 1 else None
            document = (
                await uow.source_documents.get(source.source_document_id)
                if source.source_document_id
                else None
            )
            return candidate, document, discovery_candidate_id

    async def manual_archive_receipt(self, source: SourceCollection) -> ManualArchiveReceipt | None:
        """Return the durable receipt of an analyst upload, or ``None``.

        Only a manually archived source can have one, so the provenance log is
        read for those alone.  The facts survive a reload because they live in
        ``source.archived_manually``, never in a client-held response.
        """
        if source.origin_kind is not SourceOriginKind.MANUAL:
            return None
        async with self._uow_factory() as uow:
            events = await uow.provenance.list_for_aggregate("source_collection", source.id)
        for event in sorted(events, key=lambda item: item.occurred_at, reverse=True):
            if event.event_type != "source.archived_manually":
                continue
            receipt = ManualArchiveReceipt.from_provenance(event)
            if receipt is not None:
                return receipt
        return None

    async def archive_receipt(self, source: SourceCollection) -> SourceArchiveReceipt | None:
        """Return the durable receipt for the exact document attached to a source."""
        if source.source_document_id is None:
            return None
        async with self._uow_factory() as uow:
            events = await uow.provenance.list_for_aggregate("source_collection", source.id)
        for event in sorted(events, key=lambda item: item.occurred_at, reverse=True):
            receipt = SourceArchiveReceipt.from_provenance(event)
            if receipt is not None and receipt.source_document_id == source.source_document_id:
                return receipt
        return None

    async def download_source(
        self, subject_id: UUID, collection_id: UUID
    ) -> tuple[SourceDocument, bytes]:
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get(collection_id)
            if collection is None or collection.subject_id != subject_id:
                raise CollectionItemNotFoundError(str(collection_id))
            if collection.state not in _COLLECTED_STATES or not collection.source_document_id:
                raise CollectionNotAllowedError("La source n'est pas archivée.")
            document = await uow.source_documents.get(collection.source_document_id)
            decoded_blob_id = document.decoded_blob_id if document else None
            if document is None or decoded_blob_id is None:
                raise CollectionNotAllowedError("Le contenu décodé archivé est indisponible.")
            blob = await uow.blobs.get(decoded_blob_id)
            if blob is None:
                raise CollectionNotAllowedError("Le contenu décodé archivé est indisponible.")
        content = await self._blob_store.read(
            blob.descriptor,
            max_bytes=self._collector.policy.max_expanded_bytes,
        )
        logger.info(
            "source_downloaded",
            extra={
                "event": "source.downloaded",
                "subject_id": str(subject_id),
                "source_collection_id": str(collection_id),
            },
        )
        return document, content

    async def prepare_retry(self, collection_id: UUID) -> SourceCollection:
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get_for_update(collection_id)
            if collection is None:
                raise CollectionItemNotFoundError(str(collection_id))
            attempts = await uow.collection_attempts.list_for_collection(collection_id)
            previous_policy = attempts[-1].policy_snapshot_id if attempts else None
            collection.prepare_explicit_retry(
                policy_changed=previous_policy is not None
                and previous_policy != self._policy_snapshot.id
            )
            await uow.collection_policy_snapshots.add_if_absent(self._policy_snapshot)
            await uow.source_collections.save(collection)
            await uow.commit()
            return collection

    async def fallback_acquire(
        self,
        subject_id: UUID,
        collection_id: UUID,
        job_id: UUID,
        context: JobExecutionContext,
    ) -> CollectionState:
        """Run only the configured fallback chain for an operator request."""
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get(collection_id)
            if collection is None or collection.subject_id != subject_id:
                raise CollectionItemNotFoundError(str(collection_id))
            if collection.state in _COLLECTED_STATES:
                return collection.state
            if collection.state not in {
                CollectionState.UNAVAILABLE,
                CollectionState.BLOCKED,
                CollectionState.FAILED_RETRYABLE,
                CollectionState.FAILED_TERMINAL,
            }:
                raise CollectionNotAllowedError("Source state does not allow fallback acquisition")
            if not self.fallback_acquisition_enabled:
                raise CollectionNotAllowedError("fallback acquisition disabled")
            if collection.source_document_id is not None:
                raise CollectionNotAllowedError("source already has an archived document")
        candidate = await self._candidate_for(collection)
        return await self._acquire_fallback_chain(
            collection_id,
            job_id,
            context,
            candidate=candidate,
            explicit_request=True,
        )

    async def collect_subject(
        self,
        subject_id: UUID,
        job_id: UUID,
        context: JobExecutionContext,
        *,
        collection_id: UUID | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> str:
        if collection_id is not None:
            if snapshot is not None and snapshot.subject_id != subject_id:
                raise CollectionNotAllowedError(
                    "Production snapshot does not belong to the subject"
                )
            async with self._uow_factory() as uow:
                source = await uow.source_collections.get(collection_id)
            if source is None or source.subject_id != subject_id:
                raise CollectionItemNotFoundError(str(collection_id))
            sources = [source]
        elif snapshot is not None:
            sources = await self.initialize_from_snapshot(subject_id, snapshot)
        else:
            sources = await self.initialize(subject_id)
        summary = CollectionSummary(total=len(sources))
        await context.report_progress(0, len(sources), "Préparation de la collecte")
        for index, source in enumerate(sources, start=1):
            await context.check_cancelled()
            candidate = await self._candidate_for(source, snapshot=snapshot)
            await context.report_progress(
                index - 1,
                len(sources),
                f"Préparation de la source {index}/{len(sources)}",
            )
            await context.record_diagnostics(
                {
                    "source_collection_id": str(source.id),
                    "source_candidate_id": str(source.source_candidate_id),
                    "number": index,
                    "total": len(sources),
                    "title": candidate.title if candidate else None,
                    "publisher": candidate.publisher if candidate else None,
                    "url": safe_fallback_url(source.requested_url),
                    "phase": "preparing",
                    "correlation_id": get_correlation_id(),
                }
            )
            already_archived = source.state in _COLLECTED_STATES
            if already_archived:
                try:
                    await self._ensure_archived_document_metadata(source, candidate)
                except CollectionNotAllowedError as e:
                    logger.warning(
                        f"Could not ensure archived metadata for collection {source.id}: {e}. "
                        f"Collection will be retained in its current state."
                    )
                    # Don't re-raise: allow the workflow to continue with the archived source
            if already_archived or source.state in {
                CollectionState.UNAVAILABLE,
                CollectionState.BLOCKED,
                CollectionState.FAILED_RETRYABLE,
                CollectionState.FAILED_TERMINAL,
            }:
                state = source.state
                if source.state in {
                    CollectionState.UNAVAILABLE,
                    CollectionState.BLOCKED,
                    CollectionState.FAILED_TERMINAL,
                }:
                    state = await self._acquire_fallback_chain(
                        source.id,
                        job_id,
                        context,
                        candidate=candidate,
                    )
            else:
                await context.report_progress(
                    index - 1,
                    len(sources),
                    f"Téléchargement de la source {index}/{len(sources)}",
                )
                state = await self.archive_one(
                    source.id,
                    job_id,
                    context=context,
                    position=(index, len(sources)),
                    candidate=candidate,
                )
            summary.record(state, already_archived=already_archived)
            state_label = _state_label(state)
            await context.report_progress(
                index,
                len(sources),
                f"Source {index}/{len(sources)} {state_label}",
            )
            await context.record_diagnostics(
                {
                    "source_collection_id": str(source.id),
                    "source_candidate_id": str(source.source_candidate_id),
                    "number": index,
                    "total": len(sources),
                    "title": candidate.title if candidate else None,
                    "publisher": candidate.publisher if candidate else None,
                    "url": safe_fallback_url(source.requested_url),
                    "phase": "persisted",
                    "state": state.value,
                    "correlation_id": get_correlation_id(),
                }
            )
            await context.heartbeat()
        message = _summary_message(summary)
        await context.report_progress(len(sources), len(sources), message)
        if summary.success_count:
            await self._materialize_workspace(subject_id, job_id=job_id)
        output_reference = await self._record_summary(subject_id, job_id, summary)
        logger.info(
            "source_collection_completed",
            extra={
                "event": "source.collection.completed",
                "job_id": str(job_id),
                "subject_id": str(subject_id),
                "summary": asdict(summary),
            },
        )
        if summary.success_count == 0:
            # Un échec retryable doit rester retryable : le job dispatcher
            # dispose de trois tentatives, et une collecte bloquée par un
            # ralentissement réseau réussit souvent à la seconde.
            raise JobHandlerError(
                "source_collection_no_success",
                "Aucune publication n'a pu être archivée.",
                transient=summary.failed_retryable > 0,
                details={
                    **asdict(summary),
                    "collection_failures": await self._failure_diagnostics(sources),
                },
            )
        return output_reference

    async def archive_one(
        self,
        collection_id: UUID,
        job_id: UUID,
        *,
        context: JobExecutionContext | None = None,
        position: tuple[int, int] | None = None,
        candidate: SourceCandidate | None = None,
    ) -> CollectionState:
        operation_started = time.monotonic()
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get_for_update(collection_id)
            if collection is None:
                raise CollectionItemNotFoundError(str(collection_id))
            if collection.state in _COLLECTED_STATES:
                return collection.state
            await uow.collection_policy_snapshots.add_if_absent(self._policy_snapshot)
            now = datetime.now(UTC)
            if collection.state is CollectionState.FETCHING:
                if collection.fetch_lease_expires_at and collection.fetch_lease_expires_at > now:
                    return collection.state
                await uow.collection_attempts.append(
                    _interrupted_attempt(
                        collection,
                        now,
                        fallback_job_id=job_id,
                        fallback_policy_snapshot_id=self._policy_snapshot.id,
                    )
                )
            credential_like_source = (
                collection.error_reason == _CREDENTIAL_LIKE_URL_REJECTION
                or not is_credential_free_url(collection.requested_url)
            )
            if credential_like_source:
                safe_url = credential_free_url(collection.requested_url)
                collection.requested_url = safe_url
                collection.canonical_url = safe_url
                collection.error_reason = _CREDENTIAL_LIKE_URL_REJECTION
            claimed = collection.claim_fetch(
                job_id,
                lease_duration=self._fetch_lease,
                policy_snapshot_id=self._policy_snapshot.id,
                now=now,
            )
            if not claimed:
                return collection.state
            await uow.source_collections.save(collection)
            await uow.commit()

        started_at = collection.fetch_started_at or datetime.now(UTC)
        if credential_like_source or not is_credential_free_url(collection.requested_url):
            error = UnsafeAddressError("Credential-like URL is not eligible for collection")
            state = await self._record_failure(
                collection.id,
                job_id,
                started_at,
                error,
                credential_like_rejection=True,
            )
            self._log_source_result(
                collection,
                job_id,
                state,
                operation_started,
                error=error,
            )
            return state
        try:
            response = await self._collector.fetch(
                collection.requested_url,
                cancellation_check=context.check_cancelled if context else None,
            )
        except JobCancelledError:
            await self._record_interruption(
                collection.id,
                job_id,
                "Collection cancelled before archival",
            )
            raise
        except CollectionError as exc:
            state = await self._record_failure(collection.id, job_id, started_at, exc)
            if state in {CollectionState.BLOCKED, CollectionState.UNAVAILABLE}:
                fallback_state = await self._acquire_fallback_chain(
                    collection.id,
                    job_id,
                    context,
                    candidate=candidate,
                    initial_error=exc,
                )
                self._log_source_result(
                    collection,
                    job_id,
                    fallback_state,
                    operation_started,
                    error=exc if fallback_state is not CollectionState.ARCHIVED else None,
                )
                return fallback_state
            self._log_source_result(collection, job_id, state, operation_started, error=exc)
            return state
        if not await self._renew_fetch_lease(collection.id, job_id):
            return CollectionState.FETCHING
        if context is not None:
            try:
                await context.check_cancelled()
            except JobCancelledError:
                await self._record_interruption(
                    collection.id,
                    job_id,
                    "Collection cancelled before archival",
                )
                raise
            if position:
                await context.report_progress(
                    position[0] - 1,
                    position[1],
                    f"Archivage de la source {position[0]}/{position[1]}",
                )
        processing_failure = _pdf_processing_failure(collection.requested_url, response)
        await self._archive(
            collection.id,
            started_at,
            response,
            acquisition=_CollectorAcquisition(job_id=job_id),
            candidate=candidate,
            processing_failure=processing_failure,
        )
        resulting_state = (
            _processing_failure_state(processing_failure)
            if processing_failure is not None
            else CollectionState.ARCHIVED
        )
        self._log_source_result(
            collection,
            job_id,
            resulting_state,
            operation_started,
            size=response.decoded_size,
        )
        return resulting_state

    async def archive_manual_content(
        self,
        collection_id: UUID,
        *,
        content: bytes,
        declared_mime_type: str,
        final_url: str | None = None,
        actor_id: str,
        return_receipt: bool = False,
    ) -> SourceCollection | tuple[SourceCollection, ManualArchiveReceipt]:
        """Archive analyst-supplied bytes as if they had been fetched.

        An anti-bot page, a 403 or a JavaScript-rendered article cannot be
        collected, yet the analyst holds the exact publication. The evidence
        gate only ever compares proposals to the archived text, so supplying
        that text restores the whole downstream chain — extraction, evidence
        verification, publication — with no special case anywhere else.
        """
        correlation_id = get_correlation_id()
        subject_id: UUID | None = None
        try:
            if not content:
                raise ManualContentEmptyError("Manual source content is empty")
            if len(content) > self._policy.max_download_bytes:
                raise ManualContentTooLargeError("Manual source content exceeds the download limit")
            try:
                detected_content_type = _detect_mime(content)
            except UnsupportedContentError as exc:
                raise ManualContentTypeError("Detected content type is not supported") from exc
            if not isinstance(detected_content_type, DetectedMimeType):
                raise ManualContentTypeError("Detected content type is not supported")

            # A lease token, never a job id: no row in ``jobs`` describes an
            # analyst upload, and ``source_collections.fetch_job_id`` is a foreign
            # key into that table.
            manual_lease_id = uuid4()
            started_at = datetime.now(UTC)
            async with self._uow_factory() as uow:
                collection = await uow.source_collections.get_for_update(collection_id)
                if collection is None:
                    raise CollectionItemNotFoundError(str(collection_id))
                subject_id = collection.subject_id
                fallback_document_id = collection.source_document_id
                replace_fallback = False
                if collection.state in _COLLECTED_STATES and fallback_document_id is not None:
                    events = await uow.provenance.list_for_aggregate(
                        "source_collection", collection.id
                    )
                    current_archive = next(
                        (
                            event
                            for event in sorted(
                                events, key=lambda item: item.occurred_at, reverse=True
                            )
                            if (event.payload if isinstance(event.payload, dict) else {}).get(
                                "source_document_id"
                            )
                            == str(fallback_document_id)
                        ),
                        None,
                    )
                    replace_fallback = bool(
                        current_archive and current_archive.event_type == "source.archived_fallback"
                    )
                if collection.state in _COLLECTED_STATES and not replace_fallback:
                    raise ManualContentAlreadyArchivedError("source_already_archived")
                claimed = collection.claim_manual_upload(
                    manual_lease_id,
                    lease_duration=self._fetch_lease,
                    policy_snapshot_id=self._policy_snapshot.id,
                    allow_replace_archived_fallback=replace_fallback,
                    now=started_at,
                )
                if not claimed:
                    raise CollectionNotAllowedError(
                        "Manual source content cannot be archived in the current state"
                    )
                await uow.collection_policy_snapshots.add_if_absent(self._policy_snapshot)
                await uow.source_collections.save(collection)
                await uow.commit()

            response = CollectedResponse(
                requested_url=collection.canonical_url,
                final_url=final_url or collection.canonical_url,
                redirect_chain=(),
                status=200,
                headers={},
                declared_content_type=declared_mime_type,
                detected_content_type=detected_content_type,
                encoded_body=content,
                decoded_body=content,
                encoded_size=len(content),
                encoded_sha256=hashlib.sha256(content).hexdigest(),
                decoded_size=len(content),
                decoded_sha256=hashlib.sha256(content).hexdigest(),
                content_encoding="identity",
                acquired_at=datetime.now(UTC),
            )
            processing_failure = _pdf_processing_failure(collection.requested_url, response)
            receipt = await self._archive(
                collection_id,
                started_at,
                response,
                acquisition=_ManualAcquisition(
                    manual_lease_id=manual_lease_id,
                    actor_id=actor_id,
                    declared_mime_type=declared_mime_type,
                ),
                processing_failure=processing_failure,
            )
            if receipt is None:
                raise RuntimeError("manual archive did not produce a receipt")
            # This is deliberately after the provenance/blob transaction has
            # committed. The log is metadata-only and therefore contains no
            # analyst-supplied bytes.
            logger.info(
                "source_manual_archive_completed",
                extra={"event": "source.manual_archive.completed", **receipt.log_fields()},
            )

            async with self._uow_factory() as uow:
                collection = await _require_collection(uow, collection_id)
                return (collection, receipt) if return_receipt else collection
        except Exception as exc:
            logger.warning(
                "source_manual_archive_failed",
                extra={
                    "event": "source.manual_archive.failed",
                    "subject_id": str(subject_id) if subject_id is not None else None,
                    "collection_id": str(collection_id),
                    "actor_id": actor_id,
                    "correlation_id": correlation_id,
                    "error_code": type(exc).__name__,
                },
            )
            raise

    async def _candidate_for(
        self,
        collection: SourceCollection,
        *,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> SourceCandidate | None:
        if snapshot is not None:
            return next(
                (
                    source.to_source_candidate()
                    for source in snapshot.core_sources
                    if source.canonical_url == collection.canonical_url
                ),
                None,
            )
        async with self._uow_factory() as uow:
            found, _owners = await _discovery_source(
                uow, collection.batch_id, collection.source_candidate_id
            )
            return found

    async def _ensure_archived_document_metadata(
        self, collection: SourceCollection, candidate: SourceCandidate | None
    ) -> None:
        if collection.source_document_id is None:
            raise CollectionNotAllowedError("Archived source lost its canonical metadata")

        async with self._uow_factory() as uow:
            document = await uow.source_documents.get(collection.source_document_id)
            if document is None or collection.decoded_blob_id is None:
                raise CollectionNotAllowedError("Archived source content is missing")

            # If candidate is missing but document has metadata, reuse existing metadata
            if candidate is None:
                logger.warning(
                    f"Source candidate missing for collection {collection.id}, "
                    f"reusing existing document metadata"
                )
                return
            if document.title is not None and document.logical_filename is not None:
                return
            attempts = await uow.collection_attempts.list_for_collection(collection.id)
            attempt = attempts[-1] if attempts else None
            decoded_blob = await uow.blobs.get(collection.decoded_blob_id)
            if decoded_blob is None:
                raise CollectionNotAllowedError("Archived decoded blob is missing")
            decoded_sha256 = (
                attempt.decoded_sha256
                if attempt and attempt.decoded_sha256
                else decoded_blob.descriptor.sha256
            )
            detected_mime_type = (
                attempt.detected_content_type
                if attempt and attempt.detected_content_type
                else decoded_blob.descriptor.mime_type
            )
            existing_names = {
                item.logical_filename
                for item in await uow.source_documents.list_for_subject(collection.subject_id)
                if item.id != document.id and item.logical_filename
            }
            document.logical_filename = analyst_filename(
                published_at=candidate.published_at,
                tlp=candidate.tlp,
                title=candidate.title,
                publisher=candidate.publisher,
                detected_mime_type=detected_mime_type,
                decoded_sha256=decoded_sha256,
                existing_names=existing_names,
            )
            document.source_collection_id = collection.id
            document.source_candidate_id = candidate.id
            document.decoded_blob_id = collection.decoded_blob_id
            document.title = candidate.title
            document.publisher = candidate.publisher
            document.published_at = candidate.published_at
            document.origin = collection.requested_url
            document.final_url = attempt.final_url if attempt else document.origin
            document.declared_mime_type = attempt.declared_content_type if attempt else None
            document.detected_mime_type = detected_mime_type
            document.encoded_sha256 = attempt.encoded_sha256 if attempt else None
            document.decoded_sha256 = decoded_sha256
            document.encoded_size = attempt.encoded_size if attempt else None
            document.decoded_size = (
                attempt.decoded_size if attempt else decoded_blob.descriptor.size
            )
            await uow.source_documents.save(document)
            await uow.commit()

    async def _record_summary(
        self, subject_id: UUID, job_id: UUID, summary: CollectionSummary
    ) -> str:
        async with self._uow_factory() as uow:
            subject = await uow.subjects.get(subject_id)
            if subject is None:
                raise CollectionItemNotFoundError(str(subject_id))
            event = ProvenanceEvent(
                subject_id=subject_id,
                aggregate_type="source_collection_job",
                aggregate_id=job_id,
                event_type="source.collection_completed",
                payload=asdict(summary),
                tlp=subject.tlp,
                actor_id="system:collector",
            )
            await uow.provenance.append(event)
            await uow.commit()
        return f"provenance://events/{event.id}"

    async def _materialize_workspace(self, subject_id: UUID, *, job_id: UUID | None = None) -> None:
        if self._workspace_materializer is None or self._workspace_root is None:
            return
        async with self._uow_factory() as uow:
            subject = await uow.subjects.get(subject_id)
            if subject is None:
                raise CollectionItemNotFoundError(str(subject_id))
            documents = list(await uow.source_documents.list_for_subject(subject_id))
            samples_repository = getattr(uow, "samples", None)
            samples = (
                list(await samples_repository.list_for_subject(subject_id))
                if samples_repository is not None
                else []
            )
            blob_ids = {
                blob_id
                for document in documents
                for blob_id in (document.blob_id, document.decoded_blob_id)
                if blob_id is not None
            } | {sample.blob_id for sample in samples}
            blobs = {}
            for blob_id in blob_ids:
                blob = await uow.blobs.get(blob_id)
                if blob is None:
                    raise CollectionItemNotFoundError(str(blob_id))
                blobs[blob_id] = blob
        try:
            await self._workspace_materializer.materialize(
                subject,
                documents,
                samples,
                blobs,
                self._workspace_root,
            )
        except Exception:
            logger.exception(
                "subject_workspace_materialize_failed",
                extra={
                    "operation": "subject_workspace_materialize",
                    "subject_id": str(subject_id),
                    "job_id": str(job_id) if job_id is not None else None,
                    "correlation_id": get_correlation_id(),
                },
            )

    @staticmethod
    def _log_source_result(
        collection: SourceCollection,
        job_id: UUID,
        state: CollectionState,
        started: float,
        *,
        size: int | None = None,
        error: CollectionError | None = None,
    ) -> None:
        requested_url = collection.requested_url
        if not is_credential_free_url(requested_url):
            requested_url = credential_free_url(requested_url)
        logger.info(
            "source_collection_result",
            extra={
                "event": "source.collection.persisted",
                "job_id": str(job_id),
                "subject_id": str(collection.subject_id),
                "source_collection_id": str(collection.id),
                "source_candidate_id": str(collection.source_candidate_id),
                "requested_url": requested_url,
                "phase": "persisted",
                "state": state.value,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "size": size,
                "error_code": type(error).__name__ if error else None,
            },
        )

    async def _archive(
        self,
        collection_id: UUID,
        started_at: datetime,
        response: CollectedResponse,
        *,
        acquisition: _CollectorAcquisition | _ManualAcquisition | _FallbackAcquisition,
        candidate: SourceCandidate | None = None,
        processing_failure: tuple[CollectionFailureReason, str] | None = None,
    ) -> ManualArchiveReceipt | None:
        """Persist one acquisition: same evidence, two honest audit contexts.

        Blobs, hashes, ``SourceDocument`` and the attempt are identical for a
        collector download and an analyst upload — only the acquisition token
        and the provenance event differ, because only those two record who
        actually obtained the content.
        """
        manual = acquisition if isinstance(acquisition, _ManualAcquisition) else None
        collector = acquisition if isinstance(acquisition, _CollectorAcquisition) else None
        fallback = acquisition if isinstance(acquisition, _FallbackAcquisition) else None
        raw_blob = await self._catalog.ingest(
            BytesIO(response.encoded_body),
            logical_bucket="source-raw",
            mime_type="application/octet-stream",
        )
        decoded_blob = await self._catalog.ingest(
            BytesIO(response.decoded_body),
            logical_bucket="source-decoded",
            mime_type=response.detected_content_type.value,
        )
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            subject = await uow.subjects.get(collection.subject_id)
            source = candidate
            if source is None:
                source, _owners = await _discovery_source(
                    uow, collection.batch_id, collection.source_candidate_id
                )
            if subject is None:
                raise CollectionNotAllowedError("Collection source lost its canonical context")
            if fallback is not None and (
                collection.state in _COLLECTED_STATES
                or collection.state is CollectionState.FETCHING
                or collection.source_document_id is not None
            ):
                # PostgreSQL get_for_update holds this row lock through the
                # state/pointer check and commit; a direct/manual document wins.
                return None
            existing_names = {
                item.logical_filename
                for item in await uow.source_documents.list_for_subject(collection.subject_id)
                if item.logical_filename
            }
            # A reference-research source has no discovery candidate: the
            # collection carries its own metadata snapshot instead.
            meta_title = source.title if source else (collection.title or collection.requested_url)
            meta_publisher = source.publisher if source else (collection.publisher or "")
            meta_published_at = source.published_at if source else collection.published_at
            meta_tlp = max(
                (source.tlp, collection.source_tlp) if source else (collection.source_tlp,),
                key=lambda item: tuple(TLP).index(item),
            )
            meta_external_allowed = (
                source.external_llm_allowed if source else collection.external_llm_allowed
            ) and collection.external_llm_allowed
            logical_filename = analyst_filename(
                published_at=meta_published_at,
                tlp=meta_tlp,
                title=meta_title,
                publisher=meta_publisher,
                detected_mime_type=response.detected_content_type.value,
                decoded_sha256=response.decoded_sha256,
                existing_names=existing_names,
            )
            document = SourceDocument(
                subject_id=collection.subject_id,
                blob_id=raw_blob.id,
                original_name=_original_name(response.final_url),
                origin=(
                    collection.requested_url if fallback is not None else response.requested_url
                ),
                acquired_at=response.acquired_at,
                license_restriction=None,
                tlp=meta_tlp,
                do_not_submit=collection.do_not_submit,
                external_llm_allowed=meta_external_allowed,
                logical_filename=logical_filename,
                source_collection_id=collection.id,
                source_candidate_id=source.id if source else None,
                decoded_blob_id=decoded_blob.id,
                title=meta_title,
                publisher=meta_publisher,
                published_at=meta_published_at,
                final_url=response.final_url,
                declared_mime_type=response.declared_content_type,
                detected_mime_type=response.detected_content_type.value,
                encoded_sha256=response.encoded_sha256,
                decoded_sha256=response.decoded_sha256,
                encoded_size=response.encoded_size,
                decoded_size=response.decoded_size,
            )
            attempt = _successful_attempt(
                collection,
                started_at,
                response,
                self._policy_snapshot.id,
                job_id=(
                    collector.job_id
                    if collector
                    else fallback.job_id
                    if fallback is not None
                    else None
                ),
                manual_lease_id=manual.manual_lease_id if manual else None,
                acquisition_method=(
                    SourceAcquisitionMethod.MANUAL_UPLOAD
                    if manual is not None
                    else fallback.method
                    if fallback is not None
                    else SourceAcquisitionMethod.DIRECT_HTTP
                ),
                processing_failure=processing_failure,
                strategy_name=fallback.strategy_name if fallback is not None else None,
                strategy_version=fallback.strategy_version if fallback is not None else None,
                duration_ms=fallback.duration_ms if fallback is not None else None,
                fallback=fallback,
            )
            await uow.source_documents.add(document)
            await uow.collection_attempts.append(attempt)
            if manual is not None:
                collection.archive_manual(
                    manual_lease_id=manual.manual_lease_id,
                    attempt_id=attempt.id,
                    source_document_id=document.id,
                    decoded_blob_id=decoded_blob.id,
                )
                collection.origin_kind = SourceOriginKind.MANUAL
            elif fallback is not None:
                collection.archive_fallback(
                    attempt_id=attempt.id,
                    source_document_id=document.id,
                    decoded_blob_id=decoded_blob.id,
                )
            else:
                assert collector is not None
                collection.archive(
                    job_id=collector.job_id,
                    attempt_id=attempt.id,
                    source_document_id=document.id,
                    decoded_blob_id=decoded_blob.id,
                )
            if processing_failure is not None:
                reason_code, reason = processing_failure
                collection.fail_processing(
                    reason=reason,
                    reason_code=reason_code,
                    state=_processing_failure_state(processing_failure),
                )
            await uow.source_collections.save(collection)
            evidence = {
                "attempt_id": str(attempt.id),
                "source_document_id": str(document.id),
                "encoded_sha256": response.encoded_sha256,
                "decoded_sha256": response.decoded_sha256,
                "logical_filename": logical_filename,
                "raw_blob_id": str(raw_blob.id),
                "decoded_blob_id": str(decoded_blob.id),
                "content_encoding": response.content_encoding,
                "size": response.decoded_size,
                "declared_mime_type": response.declared_content_type or "text/plain",
                "detected_mime_type": response.detected_content_type.value,
                "correlation_id": get_correlation_id(),
            }
            payload: dict[str, object]
            if manual is not None:
                # The analyst supplied this content. Recording a collector
                # download here would be a lie the audit can never undo.
                event_type = "source.archived_manually"
                actor = manual.actor_id
                payload = {
                    **evidence,
                    "acquisition_method": SourceAcquisitionMethod.MANUAL_UPLOAD.value,
                    "actor_id": manual.actor_id,
                    "manual_lease_id": str(manual.manual_lease_id),
                    "declared_mime_type": manual.declared_mime_type,
                    "requested_url": response.requested_url,
                    "original_url": response.requested_url,
                    "final_url": response.final_url,
                    "correlation_id": get_correlation_id(),
                }
            else:
                if fallback is not None:
                    event_type = "source.archived_fallback"
                    actor = "system:acquisition_fallback"
                    acquisition_method = fallback.method.value
                    payload_job_id = fallback.job_id
                else:
                    assert collector is not None
                    event_type = "source.archived"
                    actor = "system:collector"
                    acquisition_method = SourceAcquisitionMethod.DIRECT_HTTP.value
                    payload_job_id = collector.job_id
                payload = {
                    **evidence,
                    "acquisition_method": acquisition_method,
                    "job_id": str(payload_job_id),
                    "requested_url": (
                        safe_fallback_url(collection.requested_url)
                        if fallback is not None
                        else response.requested_url
                    ),
                    "original_url": (
                        safe_fallback_url(collection.requested_url)
                        if fallback is not None
                        else response.requested_url
                    ),
                    "final_url": (
                        safe_fallback_url(response.final_url)
                        if fallback is not None
                        else response.final_url
                    ),
                    "redirect_chain": list(response.redirect_chain),
                    "candidate_resolution_url": attempt.candidate_resolution_url,
                    "candidate_resolution_provenance": (
                        attempt.candidate_resolution_provenance.value
                        if attempt.candidate_resolution_provenance is not None
                        else None
                    ),
                }
                if fallback is not None:
                    fallback_publication_date = fallback.publication_date or meta_published_at
                    payload.update(
                        {
                            "strategy_name": fallback.strategy_name,
                            "strategy_version": fallback.strategy_version,
                            "duration_ms": fallback.duration_ms,
                            "http_status": attempt.http_status,
                            "bytes": response.encoded_size,
                            "sha256": response.decoded_sha256,
                            "policy_snapshot_id": self._policy_snapshot.id,
                            "acquisition_job_id": str(fallback.job_id),
                            "actor": "system:acquisition_fallback",
                            "requested_variant_url": safe_fallback_url(
                                fallback.strategy_url or response.requested_url
                            ),
                            "publisher": fallback.publisher or meta_publisher,
                            "article_title": fallback.article_title or meta_title,
                            "publication_date_if_known": (
                                fallback_publication_date.isoformat()
                                if fallback_publication_date is not None
                                else None
                            ),
                            "archive_snapshot_time": (
                                fallback.archive_snapshot_time.isoformat()
                                if fallback.archive_snapshot_time is not None
                                else None
                            ),
                            "content_type": response.declared_content_type
                            or response.detected_content_type.value,
                            "raw_blob_reference": raw_blob.descriptor.object_key,
                            "raw_blob_id": str(raw_blob.id),
                            "decoded_blob_reference": decoded_blob.descriptor.object_key,
                            "decoded_blob_id": str(decoded_blob.id),
                            "attempt_id": str(attempt.id),
                            "source_equivalence_class": (
                                fallback.source_equivalence_class.value
                                if fallback.source_equivalence_class is not None
                                else None
                            ),
                            "evidence_class": (
                                fallback.evidence_class.value
                                if fallback.evidence_class is not None
                                else None
                            ),
                            "observation_kind": (
                                fallback.observation_kind.value
                                if fallback.observation_kind is not None
                                else None
                            ),
                            "quality_assessment": _quality_assessment(fallback.quality),
                            "model_run_id": (
                                str(fallback.model_run_id)
                                if fallback.model_run_id is not None
                                else None
                            ),
                            "consultation_id": fallback.consultation_id,
                            "prompt_sha256": fallback.prompt_sha256,
                            "response_sha256": fallback.response_sha256,
                            "model_generated": fallback.model_generated,
                            "primary_source_verified": fallback.primary_source_verified,
                            "ioc_primary_verified": fallback.ioc_primary_verified,
                            "verification_status": (
                                "unverified_by_primary_source"
                                if fallback.method is SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
                                else "archived_http_content"
                            ),
                        }
                    )
                    await uow.provenance.append(
                        _fallback_attempt_event(
                            collection,
                            max((subject.tlp, meta_tlp), key=lambda item: tuple(TLP).index(item)),
                            method=fallback.method,
                            requested_url=fallback.strategy_url or response.requested_url,
                            outcome="archived",
                            attempt_id=attempt.id,
                            reason_code=None,
                            strategy_name=fallback.strategy_name,
                            strategy_version=fallback.strategy_version,
                            duration_ms=fallback.duration_ms,
                            http_status=attempt.http_status,
                            byte_count=response.encoded_size,
                            sha256=response.encoded_sha256,
                            model_run_id=fallback.model_run_id,
                            consultation_id=fallback.consultation_id,
                            prompt_sha256=fallback.prompt_sha256,
                            response_sha256=fallback.response_sha256,
                        )
                    )
            await uow.provenance.append(
                ProvenanceEvent(
                    subject_id=collection.subject_id,
                    aggregate_type="source_collection",
                    aggregate_id=collection.id,
                    event_type=event_type,
                    payload=payload,
                    tlp=max((subject.tlp, meta_tlp), key=lambda item: tuple(TLP).index(item)),
                    actor_id=actor,
                )
            )
            await uow.commit()

            if manual is None:
                return None
            return ManualArchiveReceipt(
                subject_id=collection.subject_id,
                collection_id=collection.id,
                source_document_id=document.id,
                raw_blob_id=raw_blob.id,
                decoded_blob_id=decoded_blob.id,
                encoded_sha256=response.encoded_sha256,
                decoded_sha256=response.decoded_sha256,
                bytes=response.decoded_size,
                declared_mime_type=manual.declared_mime_type,
                detected_mime_type=response.detected_content_type.value,
                actor_id=manual.actor_id,
                correlation_id=get_correlation_id(),
                completed_at=datetime.now(UTC),
            )

    async def _renew_fetch_lease(self, collection_id: UUID, job_id: UUID) -> bool:
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            renewed = collection.renew_fetch_lease(
                job_id,
                lease_duration=self._fetch_lease,
            )
            if renewed:
                await uow.source_collections.save(collection)
                await uow.commit()
            return renewed

    async def _record_interruption(
        self,
        collection_id: UUID,
        job_id: UUID,
        reason: str,
    ) -> None:
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            if collection.state is not CollectionState.FETCHING:
                return
            now = datetime.now(UTC)
            attempt = _interrupted_attempt(
                collection,
                now,
                fallback_job_id=job_id,
                fallback_policy_snapshot_id=self._policy_snapshot.id,
                reason=reason,
            )
            await uow.collection_attempts.append(attempt)
            collection.fail(
                CollectionState.FAILED_RETRYABLE,
                attempt_id=attempt.id,
                reason=reason,
                reason_code=CollectionFailureReason.INTERRUPTED,
            )
            await uow.source_collections.save(collection)
            await uow.commit()

    async def _record_failure(
        self,
        collection_id: UUID,
        job_id: UUID,
        started_at: datetime,
        error: CollectionError,
        *,
        credential_like_rejection: bool = False,
    ) -> CollectionState:
        completed_at = datetime.now(UTC)
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            credential_like_rejection = credential_like_rejection or (
                error.reason_code is CollectionFailureReason.UNSAFE_DESTINATION
                and not bridge_url_is_credential_free(collection.requested_url)
            )
            if credential_like_rejection:
                collection.requested_url = credential_free_url(collection.requested_url)
                collection.canonical_url = credential_free_url(collection.canonical_url)
            persisted_requested_url = (
                safe_fallback_url(collection.requested_url)
                if credential_like_rejection
                else collection.requested_url
            )
            persisted_final_url = (
                safe_fallback_url(error.final_url)
                if credential_like_rejection and error.final_url
                else error.final_url
            )
            persisted_redirect_chain = (
                tuple(safe_fallback_url(url) for url in error.redirect_chain)
                if credential_like_rejection
                else error.redirect_chain
            )
            failure_reason = (
                "credential-like URL rejected" if credential_like_rejection else str(error)
            )
            candidate_resolution_url = _same_site_redirect_candidate(
                collection.requested_url, error.final_url, error.redirect_chain
            )
            if credential_like_rejection and candidate_resolution_url is not None:
                candidate_resolution_url = safe_fallback_url(candidate_resolution_url)
            attempt = CollectionAttempt(
                collection_id=collection.id,
                job_id=job_id,
                policy_snapshot_id=self._policy_snapshot.id,
                requested_url=persisted_requested_url,
                final_url=persisted_final_url,
                redirect_chain=persisted_redirect_chain,
                attempted_at=started_at,
                completed_at=completed_at,
                http_status=error.http_status,
                declared_content_type=(
                    error.headers.get("content-type", "").split(";", 1)[0] or None
                ),
                detected_content_type=error.detected_content_type,
                encoded_size=error.encoded_size,
                encoded_sha256=None,
                decoded_size=None,
                decoded_sha256=None,
                content_encoding=None,
                allowed_headers=error.headers,
                outcome=error.outcome,
                failure_reason=failure_reason,
                reason_code=error.reason_code,
                transport_classification=error.transport_classification,
                candidate_resolution_url=candidate_resolution_url,
                candidate_resolution_provenance=(
                    CollectionResolutionProvenance.SAME_SITE_HTTP_REDIRECT
                    if _same_site_redirect_candidate(
                        collection.requested_url, error.final_url, error.redirect_chain
                    )
                    else None
                ),
            )
            state = (
                CollectionState.FAILED_RETRYABLE
                if error.retryable
                else CollectionState.BLOCKED
                if error.outcome is AttemptOutcome.BLOCKED
                else CollectionState.FAILED_TERMINAL
                if isinstance(error, DownloadTooLargeError)
                else CollectionState.UNAVAILABLE
            )
            await uow.collection_attempts.append(attempt)
            collection.fail(
                state,
                attempt_id=attempt.id,
                reason=failure_reason,
                reason_code=error.reason_code,
            )
            await uow.source_collections.save(collection)
            await uow.commit()
            return state

    async def _acquire_fallback_chain(
        self,
        collection_id: UUID,
        job_id: UUID,
        context: JobExecutionContext | None,
        *,
        candidate: SourceCandidate | None = None,
        initial_error: CollectionError | None = None,
        explicit_request: bool = False,
    ) -> CollectionState:
        """Run the bounded strategy registry and archive only its accepted winner."""
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get(collection_id)
            if collection is None:
                raise CollectionItemNotFoundError(str(collection_id))
            if (
                collection.state in _COLLECTED_STATES
                or collection.state is CollectionState.FETCHING
                or collection.source_document_id is not None
            ):
                return collection.state
            fallback_states = {CollectionState.BLOCKED, CollectionState.UNAVAILABLE}
            if explicit_request:
                fallback_states.update(
                    {CollectionState.FAILED_RETRYABLE, CollectionState.FAILED_TERMINAL}
                )
            if collection.state not in fallback_states:
                return collection.state
            subject = await uow.subjects.get(collection.subject_id)
            edition = await uow.editions.get(collection.edition_id)
            events = await uow.provenance.list_for_aggregate("source_collection", collection.id)
            attempts = await uow.collection_attempts.list_for_collection(collection.id)

        direct_attempt = next(
            (
                attempt
                for attempt in reversed(attempts)
                if attempt.strategy_name is None
                and attempt.requested_url == collection.canonical_url
            ),
            None,
        )
        direct_failure_reason = (
            initial_error.reason_code
            if initial_error is not None
            else direct_attempt.reason_code
            if direct_attempt is not None
            else collection.failure_reason_code
        )
        if direct_failure_reason is CollectionFailureReason.UNSAFE_DESTINATION:
            return collection.state
        if not explicit_request and direct_failure_reason not in {
            CollectionFailureReason.ACCESS_BLOCKED,
            CollectionFailureReason.HTTP_ERROR,
            CollectionFailureReason.OBSOLETE_URL_404,
        }:
            return collection.state
        if subject is None or not self._fallback_acquisition_enabled:
            return collection.state

        effective_tlp = max(
            (
                subject.tlp,
                collection.source_tlp,
                candidate.tlp if candidate is not None else collection.source_tlp,
            ),
            key=lambda item: tuple(TLP).index(item),
        )
        external_llm_allowed = collection.external_llm_allowed and (
            candidate.external_llm_allowed if candidate is not None else True
        )
        policy = AcquisitionPolicyContext(
            allowed_domains=frozenset(self._policy.allowed_domains),
            blocked_domains=frozenset(self._policy.blocked_domains),
            external_llm_allowed=external_llm_allowed,
            tlp=effective_tlp,
            do_not_submit=collection.do_not_submit,
            expected_languages=edition.languages if edition is not None else (),
            allowed_syndication_domains=self._allowed_syndication_domains,
            third_party_provider_allowed=self._third_party_provider_enabled,
            provider_configured=bool(
                self._third_party_provider_enabled
                and self._third_party_provider_url
                and self._third_party_provider_token
            ),
        )
        if policy.check_url(collection.canonical_url) is not AcquisitionPolicyDecision.ALLOWED:
            return collection.state

        attempted_strategy_urls: set[str] = set()
        bridge_events: list[ProvenanceEvent] = []
        for event in events:
            if event.event_type != "source.acquisition_fallback_attempted":
                continue
            payload = event.payload if isinstance(event.payload, dict) else {}
            strategy_name = str(
                payload.get("strategy_name") or payload.get("acquisition_method") or ""
            )
            outcome = payload.get("outcome")
            if outcome not in {
                AcquisitionOutcome.SUCCEEDED.value,
                AcquisitionOutcome.REJECTED.value,
                AcquisitionOutcome.ARCHIVED.value,
                AcquisitionOutcome.LOOKUP_SUCCEEDED.value,
                AcquisitionOutcome.METADATA_SUCCEEDED.value,
                AcquisitionOutcome.POLICY_SKIPPED.value,
                AcquisitionOutcome.NEEDS_RECONCILIATION.value,
            }:
                continue
            attempted_url_hash = payload.get("attempted_url_sha256")
            strategy_url_hash = payload.get("attempted_strategy_url_sha256")
            if isinstance(strategy_url_hash, str):
                attempted_strategy_urls.add(strategy_url_hash)
            elif isinstance(attempted_url_hash, str):
                attempted_strategy_urls.add(
                    hashlib.sha256(f"{strategy_name}\0{attempted_url_hash}".encode()).hexdigest()
                )
            if (
                payload.get("acquisition_method")
                == SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION.value
            ):
                bridge_events.append(event)

        fallback_headers = (
            initial_error.headers
            if initial_error is not None
            else direct_attempt.allowed_headers
            if direct_attempt is not None
            else {}
        )
        fallback_body = initial_error.response_body if initial_error is not None else None

        async def safe_fetch(
            url: str, headers: dict[str, str], timeout_seconds: float, allow_binary: bool
        ) -> CollectedResponse:
            if not bridge_url_is_credential_free(url):
                raise UnsafeAddressError("Credential-like URL is not eligible for fallback")
            if policy.check_url(url) is not AcquisitionPolicyDecision.ALLOWED:
                raise UnsafeAddressError("Destination is not allowed by fallback policy")
            return await self._collector.fetch(
                url,
                cancellation_check=context.check_cancelled if context else None,
                request_headers=headers,
                timeout_seconds=timeout_seconds,
                allow_binary=allow_binary,
            )

        fallback_collection_id = collection.id
        fallback_requested_url = collection.canonical_url
        bridge_status = self._bridge_fallback_status(bridge_events)

        async def record_fallback_probe(probe: AcquisitionProbe) -> None:
            await self._record_fallback_probe(fallback_collection_id, job_id, probe)

        async def bridge_attempt() -> AcquisitionProbe:
            if bridge_status in {"rejected", "skipped"}:
                return AcquisitionProbe(
                    strategy_name="bridge_transcription",
                    strategy_version="1",
                    method=SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION,
                    requested_url=collection.canonical_url,
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason_code="prior_bridge_attempt_not_replayable",
                    response=None,
                    http_status=None,
                    encoded_size=None,
                    encoded_sha256=None,
                    duration_ms=0,
                )
            return await self._bridge_transcribe(
                collection,
                subject,
                candidate,
                job_id,
                context,
                request,
                expected_languages=edition.languages if edition is not None else (),
            )

        async def bridge_discovery_attempt(max_candidates: int) -> Sequence[AcquisitionProbe]:
            remaining_attempts = max(0, self._acquisition_max_attempts - attempted_count)
            return await self._bridge_source_discover(
                collection,
                subject,
                candidate,
                job_id,
                context,
                request,
                max_candidates=min(max_candidates, remaining_attempts),
                expected_languages=edition.languages if edition is not None else (),
            )

        request = AcquisitionRequest(
            requested_url=collection.canonical_url,
            title=candidate.title if candidate else collection.title,
            published_at=candidate.published_at if candidate else collection.published_at,
            response_body=fallback_body,
            response_status=(
                initial_error.http_status
                if initial_error is not None
                else direct_attempt.http_status
                if direct_attempt is not None
                else None
            ),
            response_content_type=(
                initial_error.headers.get("content-type")
                if initial_error is not None
                else direct_attempt.declared_content_type
                if direct_attempt is not None
                else None
            ),
            response_headers=dict(initial_error.headers) if initial_error is not None else {},
            link_header=fallback_headers.get("link"),
            fetch=safe_fetch,
            bridge=bridge_attempt,
            bridge_discovery=bridge_discovery_attempt,
            publisher=candidate.publisher if candidate else collection.publisher,
            attempted_strategy_urls=frozenset(attempted_strategy_urls),
            min_text_chars=self._fallback_min_text_chars,
            expected_languages=edition.languages if edition is not None else (),
            policy=policy,
        )

        # Run Bridge discovery after direct archive methods and before transcription.
        configured = (
            tuple(
                strategy
                for strategy in self._fallback_strategies
                if strategy.method
                not in {
                    SourceAcquisitionMethod.BRIDGE_SOURCE_DISCOVERY,
                    SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION,
                }
            )
            + tuple(
                strategy
                for strategy in self._fallback_strategies
                if strategy.method is SourceAcquisitionMethod.BRIDGE_SOURCE_DISCOVERY
            )
            + tuple(
                strategy
                for strategy in self._fallback_strategies
                if strategy.method is SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
            )
        )
        handled_probe_ids: set[int] = set()
        deferred_success_probes: dict[int, AcquisitionProbe] = {}
        attempted_count = 0
        attempt_limit = self._acquisition_max_attempts

        class RecordingStrategy:
            """Persist non-winning diagnostics before a later callback can archive."""

            def __init__(self, strategy: AcquisitionStrategy) -> None:
                self.inner = strategy
                self.name = strategy.name
                self.version = strategy.version
                self.method = strategy.method
                self.enabled = True
                self.timeout_seconds = float(strategy.timeout_seconds) + 0.05
                self.capability_state = AcquisitionCapabilityState.IMPLEMENTED

            async def acquire(
                self, acquisition_request: AcquisitionRequest
            ) -> tuple[AcquisitionProbe, ...]:
                nonlocal attempted_count
                strategy = self.inner
                probes: tuple[AcquisitionProbe, ...]
                if not strategy.enabled:
                    disabled_capability = getattr(
                        strategy,
                        "capability_state",
                        AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION,
                    )
                    disabled_outcome = getattr(
                        strategy, "capability_outcome", AcquisitionOutcome.SKIPPED
                    )
                    probes = (
                        AcquisitionProbe(
                            strategy_name=strategy.name,
                            strategy_version=strategy.version,
                            method=strategy.method,
                            requested_url=fallback_requested_url,
                            outcome=disabled_outcome,
                            reason_code=getattr(strategy, "capability_reason", "strategy_disabled"),
                            response=None,
                            http_status=None,
                            encoded_size=None,
                            encoded_sha256=None,
                            duration_ms=0,
                            capability_state=disabled_capability,
                        ),
                    )
                else:
                    capability = getattr(
                        strategy, "capability_state", AcquisitionCapabilityState.IMPLEMENTED
                    )
                    if capability is not AcquisitionCapabilityState.IMPLEMENTED:
                        is_policy_block = capability in {
                            AcquisitionCapabilityState.BLOCKED_BY_POLICY,
                            AcquisitionCapabilityState.BLOCKED_BY_SECURITY,
                        }
                        configured_outcome = getattr(strategy, "capability_outcome", None)
                        probes = (
                            AcquisitionProbe(
                                strategy_name=strategy.name,
                                strategy_version=strategy.version,
                                method=strategy.method,
                                requested_url=fallback_requested_url,
                                outcome=(
                                    configured_outcome
                                    if isinstance(configured_outcome, AcquisitionOutcome)
                                    else AcquisitionOutcome.POLICY_SKIPPED
                                    if is_policy_block
                                    else AcquisitionOutcome.NOT_CONFIGURED
                                ),
                                reason_code=getattr(
                                    strategy, "capability_reason", capability.value
                                ),
                                response=None,
                                http_status=None,
                                encoded_size=None,
                                encoded_sha256=None,
                                duration_ms=0,
                                policy_decision=(
                                    getattr(
                                        strategy,
                                        "capability_policy_decision",
                                        AcquisitionPolicyDecision.DENIED_PRIVATE_DESTINATION,
                                    )
                                    if capability is AcquisitionCapabilityState.BLOCKED_BY_SECURITY
                                    else AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY
                                    if is_policy_block
                                    else AcquisitionPolicyDecision.NOT_CONFIGURED
                                ),
                                capability_state=capability,
                            ),
                        )
                    else:
                        started = time.monotonic()
                        try:
                            async with asyncio.timeout(float(strategy.timeout_seconds)):
                                probes = tuple(await strategy.acquire(acquisition_request))
                        except JobCancelledError:
                            raise
                        except TimeoutError:
                            probes = (
                                AcquisitionProbe(
                                    strategy_name=strategy.name,
                                    strategy_version=strategy.version,
                                    method=strategy.method,
                                    requested_url=fallback_requested_url,
                                    outcome=AcquisitionOutcome.FAILED,
                                    reason_code="strategy_timeout",
                                    response=None,
                                    http_status=None,
                                    encoded_size=None,
                                    encoded_sha256=None,
                                    duration_ms=round((time.monotonic() - started) * 1000),
                                ),
                            )
                        except Exception as error:
                            probes = (
                                AcquisitionProbe(
                                    strategy_name=strategy.name,
                                    strategy_version=strategy.version,
                                    method=strategy.method,
                                    requested_url=fallback_requested_url,
                                    outcome=AcquisitionOutcome.FAILED,
                                    reason_code=f"strategy_error:{type(error).__name__}",
                                    response=None,
                                    http_status=None,
                                    encoded_size=None,
                                    encoded_sha256=None,
                                    duration_ms=round((time.monotonic() - started) * 1000),
                                ),
                            )

                bounded_probes: list[AcquisitionProbe] = []
                for probe in probes:
                    counts_as_attempt = probe.outcome in {
                        AcquisitionOutcome.SUCCEEDED,
                        AcquisitionOutcome.FAILED,
                        AcquisitionOutcome.REJECTED,
                        AcquisitionOutcome.NEEDS_RECONCILIATION,
                        AcquisitionOutcome.LOOKUP_SUCCEEDED,
                        AcquisitionOutcome.METADATA_SUCCEEDED,
                        AcquisitionOutcome.ARCHIVED,
                    }
                    if counts_as_attempt and attempted_count >= attempt_limit:
                        continue
                    if counts_as_attempt:
                        attempted_count += 1
                    bounded_probes.append(probe)
                    if probe.method is SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION:
                        if (
                            probe.outcome is AcquisitionOutcome.SUCCEEDED
                            and probe.response is not None
                        ):
                            deferred_success_probes[id(probe)] = probe
                        else:
                            await record_fallback_probe(probe)
                        handled_probe_ids.add(id(probe))
                    elif (
                        probe.outcome == AcquisitionOutcome.SUCCEEDED
                        and probe.response is not None
                        and probe.policy_decision is AcquisitionPolicyDecision.ALLOWED
                    ):
                        deferred_success_probes[id(probe)] = probe
                    else:
                        await record_fallback_probe(probe)
                        handled_probe_ids.add(id(probe))
                return tuple(bounded_probes)

        orchestrator = ArticleAcquisitionOrchestrator(
            tuple(RecordingStrategy(strategy) for strategy in configured),
            deadline_seconds=self._acquisition_deadline_seconds,
            max_attempts=self._acquisition_max_attempts,
            per_domain_concurrency=self._acquisition_per_domain_concurrency,
            domain_limiter=self._acquisition_domain_limiter,
        )
        result = await orchestrator.run(
            request,
            cancellation_check=context.check_cancelled if context is not None else None,
        )
        for probe in result.probes:
            if id(probe) not in handled_probe_ids and probe is not result.winner:
                await self._record_fallback_probe(collection.id, job_id, probe)
                handled_probe_ids.add(id(probe))
            await self._fallback_diagnostic(
                context,
                collection,
                method=probe.method,
                state=probe.outcome,
                reason_code=probe.reason_code,
                requested_url=probe.requested_url,
                strategy_name=probe.strategy_name,
                strategy_version=probe.strategy_version,
                duration_ms=probe.duration_ms,
                http_status=probe.http_status,
                byte_count=probe.encoded_size,
                sha256=probe.encoded_sha256,
            )

        for probe_id, probe in deferred_success_probes.items():
            if probe_id != id(result.winner) and probe_id not in handled_probe_ids:
                await self._record_fallback_probe(collection.id, job_id, probe)

        winner = result.winner
        if winner is not None and winner.response is not None:
            response = replace(
                winner.response,
                requested_url=safe_fallback_url(winner.response.requested_url),
                final_url=safe_fallback_url(winner.response.final_url),
                redirect_chain=tuple(
                    safe_fallback_url(item) for item in winner.response.redirect_chain
                ),
            )
            quality = winner.quality
            await self._archive(
                collection.id,
                datetime.now(UTC) - timedelta(milliseconds=winner.duration_ms),
                response,
                acquisition=_FallbackAcquisition(
                    job_id=job_id,
                    method=winner.method,
                    model_run_id=UUID(winner.model_run_id) if winner.model_run_id else None,
                    consultation_id=winner.consultation_id,
                    prompt_sha256=winner.prompt_sha256,
                    response_sha256=winner.response_sha256,
                    strategy_name=winner.strategy_name,
                    strategy_version=winner.strategy_version,
                    duration_ms=winner.duration_ms,
                    strategy_url=winner.requested_url,
                    source_equivalence_class=winner.equivalence_class,
                    evidence_class=winner.evidence_class,
                    observation_kind=winner.observation_kind,
                    quality=quality,
                    archive_snapshot_time=winner.archive_snapshot_time,
                    publication_date=winner.publication_date,
                    publisher=winner.publisher,
                    article_title=candidate.title if candidate else collection.title,
                    model_generated=winner.model_generated,
                    primary_source_verified=winner.primary_source_verified,
                    ioc_primary_verified=winner.ioc_primary_verified,
                ),
                candidate=candidate,
            )
            async with self._uow_factory() as uow:
                current = await uow.source_collections.get(collection.id)
            return current.state if current is not None else collection.state

        async with self._uow_factory() as uow:
            current = await uow.source_collections.get(collection.id)
        return current.state if current is not None else collection.state

    async def _bridge_execution(
        self,
        collection: SourceCollection,
        method: SourceAcquisitionMethod,
        *,
        purpose: str,
        prompt_template_id: str,
        prompt_template_version: str,
        prompt: str,
        expected_tlp: TLP,
        policy: AcquisitionPolicyContext,
    ) -> tuple[ModelExecution | None, AcquisitionProbe | None, str, UUID]:
        """Run one deterministic Bridge operation without persisting acquisition state."""
        started = time.monotonic()
        strategy_name = method.value
        model_run_id = uuid5(
            _FALLBACK_MODEL_RUN_NAMESPACE,
            str(collection.id)
            if method is SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
            else f"{collection.id}:{purpose}",
        )
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        policy_decision = policy.check_destination(collection.canonical_url, external_llm=True)
        reason = None
        outcome = AcquisitionOutcome.POLICY_SKIPPED
        if policy_decision is not AcquisitionPolicyDecision.ALLOWED:
            reason = f"bridge_policy_denied:{policy_decision.value}"
        elif self._model_gateway is None:
            policy_decision = AcquisitionPolicyDecision.NOT_CONFIGURED
            outcome = AcquisitionOutcome.NOT_CONFIGURED
            reason = "model_gateway_unavailable"
        if reason is not None:
            return (
                None,
                AcquisitionProbe(
                    strategy_name=strategy_name,
                    strategy_version="1",
                    method=method,
                    requested_url=collection.canonical_url,
                    outcome=outcome,
                    reason_code=reason,
                    response=None,
                    http_status=None,
                    encoded_size=None,
                    encoded_sha256=None,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    model_run_id=str(model_run_id),
                    prompt_sha256=prompt_sha256,
                    policy_decision=policy_decision,
                ),
                prompt_sha256,
                model_run_id,
            )

        request = ModelRequest(
            text=prompt,
            prompt_template_id=prompt_template_id,
            prompt_template_version=prompt_template_version,
            evidence_pack_hash=prompt_sha256,
            external_llm_allowed=True,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            sensitivity=expected_tlp.value,
            web_search=True,
            run_id=model_run_id,
            metadata={
                "collection_id": str(collection.id),
                "acquisition_method": method.value,
            },
            parameters={"purpose": purpose},
        )
        assert self._model_gateway is not None
        try:
            existing_run = await self._model_gateway.get_run(model_run_id)
            if (
                existing_run is not None
                and getattr(existing_run.status, "value", existing_run.status) == "failed"
                and getattr(
                    existing_run.submission_state,
                    "value",
                    existing_run.submission_state,
                )
                == "not_submitted"
            ):
                request = replace(request, allow_failed_resubmit=True)
            if (
                existing_run is not None
                and getattr(existing_run.status, "value", "") in {"needs_review", "running"}
                and getattr(existing_run.submission_state, "value", "") != "not_submitted"
            ):
                reconciliation = await self._model_gateway.reconcile_bridge_submission(model_run_id)
                if reconciliation != "captured_output":
                    consultation_id = existing_run.bridge_request_id or existing_run.response_id
                    return (
                        None,
                        AcquisitionProbe(
                            strategy_name=strategy_name,
                            strategy_version="1",
                            method=method,
                            requested_url=collection.canonical_url,
                            outcome=AcquisitionOutcome.NEEDS_RECONCILIATION,
                            reason_code="bridge_submission_reconciliation_required",
                            response=None,
                            http_status=None,
                            encoded_size=None,
                            encoded_sha256=None,
                            duration_ms=round((time.monotonic() - started) * 1000),
                            model_run_id=str(model_run_id),
                            consultation_id=consultation_id,
                            prompt_sha256=prompt_sha256,
                        ),
                        prompt_sha256,
                        model_run_id,
                    )
            execution = await self._model_gateway.research(request)
        except JobCancelledError:
            raise
        except Exception as error:
            code = getattr(error, "code", "")
            reconciliation_required = code == "model_submission_reconciliation_required"
            return (
                None,
                AcquisitionProbe(
                    strategy_name=strategy_name,
                    strategy_version="1",
                    method=method,
                    requested_url=collection.canonical_url,
                    outcome=(
                        AcquisitionOutcome.NEEDS_RECONCILIATION
                        if reconciliation_required
                        else AcquisitionOutcome.FAILED
                    ),
                    reason_code=(
                        "bridge_submission_reconciliation_required"
                        if reconciliation_required
                        else f"bridge_execution_failed:{type(error).__name__}"
                    ),
                    response=None,
                    http_status=None,
                    encoded_size=None,
                    encoded_sha256=None,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    model_run_id=str(getattr(error, "model_run_id", None) or model_run_id),
                    prompt_sha256=prompt_sha256,
                ),
                prompt_sha256,
                model_run_id,
            )

        run_status = getattr(execution.run.status, "value", execution.run.status)
        if run_status in {"needs_review", "waiting_background", "running"}:
            return (
                None,
                AcquisitionProbe(
                    strategy_name=strategy_name,
                    strategy_version="1",
                    method=method,
                    requested_url=collection.canonical_url,
                    outcome=AcquisitionOutcome.NEEDS_RECONCILIATION,
                    reason_code="bridge_submission_reconciliation_required",
                    response=None,
                    http_status=None,
                    encoded_size=None,
                    encoded_sha256=None,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    model_run_id=str(execution.run.id),
                    consultation_id=execution.run.bridge_request_id or execution.run.response_id,
                    prompt_sha256=prompt_sha256,
                ),
                prompt_sha256,
                model_run_id,
            )
        if execution.run.id != model_run_id or not execution.output_text:
            return (
                None,
                AcquisitionProbe(
                    strategy_name=strategy_name,
                    strategy_version="1",
                    method=method,
                    requested_url=collection.canonical_url,
                    outcome=AcquisitionOutcome.FAILED,
                    reason_code="bridge_output_missing_or_run_identity_mismatch",
                    response=None,
                    http_status=None,
                    encoded_size=None,
                    encoded_sha256=None,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    model_run_id=str(execution.run.id),
                    consultation_id=execution.run.bridge_request_id or execution.run.response_id,
                    prompt_sha256=prompt_sha256,
                    response_sha256=execution.run.raw_output_sha256,
                ),
                prompt_sha256,
                model_run_id,
            )
        raw_bytes = execution.output_text.encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if execution.run.raw_output_sha256 and execution.run.raw_output_sha256 != raw_sha256:
            return (
                None,
                AcquisitionProbe(
                    strategy_name=strategy_name,
                    strategy_version="1",
                    method=method,
                    requested_url=collection.canonical_url,
                    outcome=AcquisitionOutcome.FAILED,
                    reason_code="bridge_output_hash_mismatch",
                    response=None,
                    http_status=None,
                    encoded_size=len(raw_bytes),
                    encoded_sha256=raw_sha256,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    model_run_id=str(execution.run.id),
                    consultation_id=execution.run.bridge_request_id or execution.run.response_id,
                    prompt_sha256=prompt_sha256,
                    response_sha256=execution.run.raw_output_sha256,
                ),
                prompt_sha256,
                model_run_id,
            )
        return execution, None, prompt_sha256, model_run_id

    async def _bridge_transcribe(
        self,
        collection: SourceCollection,
        subject: object,
        candidate: SourceCandidate | None,
        job_id: UUID,
        context: JobExecutionContext | None,
        acquisition_request: AcquisitionRequest,
        *,
        expected_languages: tuple[str, ...] = (),
    ) -> AcquisitionProbe:
        del subject, job_id, context
        method = SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
        title = candidate.title if candidate else collection.title
        prompt = bridge_transcription_prompt(url=collection.canonical_url, title=title)
        # The request policy was constructed from subject, collection and
        # candidate metadata by _acquire_fallback_chain.
        execution, failure, prompt_sha256, _model_run_id = await self._bridge_execution(
            collection,
            method,
            purpose="public_article_transcription",
            prompt_template_id="source-acquisition-bridge-transcription",
            prompt_template_version="2",
            prompt=prompt,
            expected_tlp=acquisition_request.policy.tlp,
            policy=acquisition_request.policy,
        )
        if failure is not None:
            return failure
        assert execution is not None and execution.output_text is not None
        raw_bytes = execution.output_text.encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        parsed = parse_bridge_transcription(execution.output_text)
        consultation_id = execution.run.bridge_request_id or execution.run.response_id
        elapsed = max(0, int(getattr(execution.run, "duration_ms", 0) or 0))
        if not parsed.parsed:
            return AcquisitionProbe(
                strategy_name="bridge_transcription",
                strategy_version="1",
                method=method,
                requested_url=collection.canonical_url,
                outcome=AcquisitionOutcome.REJECTED,
                reason_code=parsed.reason or "bridge_response_unparseable",
                response=None,
                http_status=None,
                encoded_size=len(raw_bytes),
                encoded_sha256=raw_sha256,
                duration_ms=elapsed,
                model_run_id=str(execution.run.id),
                consultation_id=consultation_id,
                prompt_sha256=prompt_sha256,
                response_sha256=execution.run.raw_output_sha256 or raw_sha256,
                equivalence_class=SourceEquivalenceClass.UNKNOWN_MATCH,
                evidence_class=EvidenceClass.MODEL_CONTEXTUAL_UNVERIFIED,
                observation_kind=AcquisitionObservationKind.MODEL_TRANSCRIPTION,
                model_generated=True,
            )
        article_bytes = parsed.article_text.encode("utf-8")
        rejection = article_rejection_reason(
            body=article_bytes,
            content_type="text/plain",
            requested_url=collection.canonical_url,
            expected_title=title,
            min_text_chars=self._fallback_min_text_chars,
            expected_languages=expected_languages,
        )
        quality = deterministic_acquisition_quality(
            parsed.article_text,
            expected_title=parsed.title or title,
            expected_languages=expected_languages,
            reason="model_transcription_unverified",
        )
        if rejection is not None:
            return AcquisitionProbe(
                strategy_name="bridge_transcription",
                strategy_version="1",
                method=method,
                requested_url=collection.canonical_url,
                outcome=AcquisitionOutcome.REJECTED,
                reason_code=f"transcript_rejected:{rejection}",
                response=None,
                http_status=None,
                encoded_size=len(raw_bytes),
                encoded_sha256=raw_sha256,
                duration_ms=elapsed,
                model_run_id=str(execution.run.id),
                consultation_id=consultation_id,
                prompt_sha256=prompt_sha256,
                response_sha256=execution.run.raw_output_sha256 or raw_sha256,
                equivalence_class=SourceEquivalenceClass.UNKNOWN_MATCH,
                evidence_class=EvidenceClass.MODEL_CONTEXTUAL_UNVERIFIED,
                observation_kind=AcquisitionObservationKind.MODEL_TRANSCRIPTION,
                quality=quality,
                model_generated=True,
            )
        response = CollectedResponse(
            requested_url=collection.canonical_url,
            final_url=collection.canonical_url,
            redirect_chain=(),
            status=0,
            headers={},
            declared_content_type="text/plain; charset=utf-8",
            detected_content_type=DetectedMimeType.TEXT,
            encoded_body=raw_bytes,
            decoded_body=article_bytes,
            encoded_size=len(raw_bytes),
            encoded_sha256=raw_sha256,
            decoded_size=len(article_bytes),
            decoded_sha256=hashlib.sha256(article_bytes).hexdigest(),
            content_encoding="identity",
            acquired_at=datetime.now(UTC),
        )
        return AcquisitionProbe(
            strategy_name="bridge_transcription",
            strategy_version="1",
            method=method,
            requested_url=collection.canonical_url,
            outcome=AcquisitionOutcome.SUCCEEDED,
            reason_code=None,
            response=response,
            http_status=None,
            encoded_size=len(raw_bytes),
            encoded_sha256=raw_sha256,
            duration_ms=elapsed,
            model_run_id=str(execution.run.id),
            consultation_id=consultation_id,
            prompt_sha256=prompt_sha256,
            response_sha256=execution.run.raw_output_sha256 or raw_sha256,
            equivalence_class=SourceEquivalenceClass.UNKNOWN_MATCH,
            evidence_class=EvidenceClass.MODEL_CONTEXTUAL_UNVERIFIED,
            observation_kind=AcquisitionObservationKind.MODEL_TRANSCRIPTION,
            quality=quality,
            publication_date=candidate.published_at if candidate else collection.published_at,
            publisher=parsed.publisher
            or (candidate.publisher if candidate else collection.publisher),
            model_generated=True,
            primary_source_verified=False,
            ioc_primary_verified=False,
        )

    async def _bridge_source_discover(
        self,
        collection: SourceCollection,
        subject: object,
        candidate: SourceCandidate | None,
        job_id: UUID,
        context: JobExecutionContext | None,
        acquisition_request: AcquisitionRequest,
        *,
        max_candidates: int,
        expected_languages: tuple[str, ...] = (),
    ) -> tuple[AcquisitionProbe, ...]:
        del context, job_id
        method = SourceAcquisitionMethod.BRIDGE_SOURCE_DISCOVERY
        title = candidate.title if candidate else collection.title
        publisher = candidate.publisher if candidate else collection.publisher
        published_at = candidate.published_at if candidate else collection.published_at
        prompt = bridge_source_discovery_prompt(
            url=collection.canonical_url,
            title=title,
            publisher=publisher,
            published_at=published_at,
        )
        del subject
        request_tlp = acquisition_request.policy.tlp
        execution, failure, prompt_sha256, model_run_id = await self._bridge_execution(
            collection,
            method,
            purpose="public_article_source_discovery",
            prompt_template_id="source-acquisition-bridge-source-discovery",
            prompt_template_version="1",
            prompt=prompt,
            expected_tlp=request_tlp,
            policy=acquisition_request.policy,
        )
        if failure is not None:
            return (failure,)
        assert execution is not None and execution.output_text is not None
        raw_bytes = execution.output_text.encode("utf-8")
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        response_sha256 = execution.run.raw_output_sha256 or raw_sha256
        locations = parse_bridge_source_locations(execution.output_text)
        duration_ms = max(0, int(getattr(execution.run, "duration_ms", 0) or 0))
        model_probe = AcquisitionProbe(
            strategy_name="bridge_source_discovery",
            strategy_version="1",
            method=method,
            requested_url=collection.canonical_url,
            outcome=(
                AcquisitionOutcome.LOOKUP_SUCCEEDED if locations else AcquisitionOutcome.REJECTED
            ),
            reason_code=None if locations else "no_candidate_locations",
            response=None,
            http_status=None,
            encoded_size=len(raw_bytes),
            encoded_sha256=raw_sha256,
            duration_ms=duration_ms,
            model_run_id=str(execution.run.id),
            consultation_id=execution.run.bridge_request_id or execution.run.response_id,
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
            policy_decision=AcquisitionPolicyDecision.ALLOWED,
            equivalence_class=SourceEquivalenceClass.UNKNOWN_MATCH,
            evidence_class=EvidenceClass.MODEL_CONTEXTUAL_UNVERIFIED,
            model_generated=True,
            primary_source_verified=False,
            ioc_primary_verified=False,
        )
        # Candidate content probes carry the discovery run and response hashes.
        # The model-only lookup has no HTTP response and therefore cannot be
        # represented as a successful CollectionAttempt without inventing a
        # final URL or content hash. Keep it as a rejection only when there are
        # no locations to fetch.
        probes: list[AcquisitionProbe] = [] if locations else [model_probe]
        seen_urls: set[str] = set()
        candidate_budget = max(0, min(max_candidates, len(locations)))
        for location in locations[:candidate_budget]:
            try:
                candidate_url = canonicalize_http_url(location.url)
            except (TypeError, ValueError):
                probes.append(
                    AcquisitionProbe(
                        strategy_name="bridge_source_discovery",
                        strategy_version="1",
                        method=method,
                        requested_url="invalid-url",
                        outcome=AcquisitionOutcome.REJECTED,
                        reason_code="candidate_url_invalid",
                        response=None,
                        http_status=None,
                        encoded_size=None,
                        encoded_sha256=None,
                        duration_ms=0,
                        model_run_id=str(model_run_id),
                        consultation_id=model_probe.consultation_id,
                        prompt_sha256=prompt_sha256,
                        response_sha256=response_sha256,
                    )
                )
                continue
            if candidate_url in seen_urls:
                continue
            seen_urls.add(candidate_url)
            decision = (
                AcquisitionPolicyDecision.DENIED_CREDENTIALS
                if not bridge_url_is_credential_free(candidate_url)
                else acquisition_request.policy.check_url(candidate_url)
            )
            metadata_reason = bridge_location_metadata_rejection(location, acquisition_request)
            if decision is not AcquisitionPolicyDecision.ALLOWED or metadata_reason is not None:
                probes.append(
                    AcquisitionProbe(
                        strategy_name="bridge_source_discovery",
                        strategy_version="1",
                        method=method,
                        requested_url=safe_fallback_url(candidate_url),
                        outcome=(
                            AcquisitionOutcome.POLICY_SKIPPED
                            if decision is not AcquisitionPolicyDecision.ALLOWED
                            else AcquisitionOutcome.REJECTED
                        ),
                        reason_code=(
                            decision.value
                            if decision is not AcquisitionPolicyDecision.ALLOWED
                            else metadata_reason
                        ),
                        response=None,
                        http_status=None,
                        encoded_size=None,
                        encoded_sha256=None,
                        duration_ms=0,
                        model_run_id=str(model_run_id),
                        consultation_id=model_probe.consultation_id,
                        prompt_sha256=prompt_sha256,
                        response_sha256=response_sha256,
                        policy_decision=decision,
                    )
                )
                continue
            attempt_key = hashlib.sha256(
                f"bridge_source_discovery\0{candidate_url}".encode()
            ).hexdigest()
            if attempt_key in acquisition_request.attempted_strategy_urls:
                probes.append(
                    AcquisitionProbe(
                        strategy_name="bridge_source_discovery",
                        strategy_version="1",
                        method=method,
                        requested_url=safe_fallback_url(candidate_url),
                        outcome=AcquisitionOutcome.SKIPPED,
                        reason_code="already_attempted_for_strategy",
                        response=None,
                        http_status=None,
                        encoded_size=None,
                        encoded_sha256=None,
                        duration_ms=0,
                        model_run_id=str(model_run_id),
                        consultation_id=model_probe.consultation_id,
                        prompt_sha256=prompt_sha256,
                        response_sha256=response_sha256,
                    )
                )
                continue
            started = time.monotonic()
            try:
                response = await acquisition_request.fetch(candidate_url, {}, 15.0, False)
            except JobCancelledError:
                raise
            except (CollectionError, PermissionError, ValueError) as error:
                probes.append(
                    AcquisitionProbe(
                        strategy_name="bridge_source_discovery",
                        strategy_version="1",
                        method=SourceAcquisitionMethod.DIRECT_HTTP,
                        requested_url=candidate_url,
                        outcome=AcquisitionOutcome.FAILED,
                        reason_code=f"candidate_fetch_failed:{type(error).__name__}",
                        response=None,
                        http_status=getattr(error, "http_status", None),
                        encoded_size=getattr(error, "encoded_size", None),
                        encoded_sha256=None,
                        duration_ms=round((time.monotonic() - started) * 1000),
                        model_run_id=str(model_run_id),
                        consultation_id=model_probe.consultation_id,
                        prompt_sha256=prompt_sha256,
                        response_sha256=response_sha256,
                    )
                )
                continue
            response_policy = acquisition_request.policy.check_url(response.final_url)
            rejection = (
                "redirect_destination_policy_denied"
                if response_policy is not AcquisitionPolicyDecision.ALLOWED
                else bridge_article_response_rejection(
                    response,
                    location=location,
                    request=acquisition_request,
                )
            )
            same_publisher = _same_acquisition_publisher(
                collection.canonical_url, response.final_url
            )
            equivalence = (
                SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
                if same_publisher
                else SourceEquivalenceClass.INDEPENDENT_CORROBORATION
            )
            observed_text = response.decoded_body.decode("utf-8", errors="replace")
            quality = deterministic_acquisition_quality(
                observed_text,
                expected_title=location.title or title,
                expected_languages=expected_languages,
                reason="bridge_discovery_content_observed",
            )
            probes.append(
                AcquisitionProbe(
                    strategy_name="bridge_source_discovery",
                    strategy_version="1",
                    method=(
                        SourceAcquisitionMethod.DIRECT_HTTP
                        if equivalence is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
                        else SourceAcquisitionMethod.INDEPENDENT_CORROBORATION
                    ),
                    requested_url=candidate_url,
                    outcome=(
                        AcquisitionOutcome.REJECTED
                        if rejection is not None
                        else AcquisitionOutcome.SUCCEEDED
                    ),
                    reason_code=rejection,
                    response=response,
                    http_status=response.status,
                    encoded_size=response.encoded_size,
                    encoded_sha256=response.encoded_sha256,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    model_run_id=str(model_run_id),
                    consultation_id=model_probe.consultation_id,
                    prompt_sha256=prompt_sha256,
                    response_sha256=response_sha256,
                    policy_decision=response_policy,
                    observation_kind=AcquisitionObservationKind.HTTP_RESPONSE,
                    equivalence_class=equivalence,
                    evidence_class=evidence_class_for_equivalence(equivalence),
                    quality=quality,
                    publication_date=published_at,
                    publisher=location.publisher or publisher,
                    model_generated=False,
                    primary_source_verified=same_publisher and rejection is None,
                    ioc_primary_verified=same_publisher and rejection is None,
                )
            )
        return tuple(probes)

    async def _record_fallback_failure(
        self,
        collection_id: UUID,
        job_id: UUID,
        *,
        requested_url: str,
        method: SourceAcquisitionMethod,
        reason: str,
        reason_code: CollectionFailureReason,
        outcome: str = "failed",
        final_url: str | None = None,
        redirect_chain: Sequence[str] = (),
        http_status: int | None = None,
        declared_content_type: str | None = None,
        detected_content_type: str | None = None,
        encoded_size: int | None = None,
        allowed_headers: dict[str, str] | None = None,
        model_run_id: UUID | None = None,
        consultation_id: str | None = None,
        prompt_sha256: str | None = None,
        response_sha256: str | None = None,
        duration_ms: int | None = None,
        strategy_name: str | None = None,
        strategy_version: str = "1",
        encoded_sha256: str | None = None,
        decoded_size: int | None = None,
        decoded_sha256: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            subject = await uow.subjects.get(collection.subject_id)
            if subject is None or collection.state in (
                _COLLECTED_STATES | {CollectionState.FETCHING}
            ):
                return
            await uow.collection_policy_snapshots.add_if_absent(self._policy_snapshot)
            attempt = CollectionAttempt(
                collection_id=collection.id,
                job_id=job_id,
                manual_lease_id=None,
                policy_snapshot_id=self._policy_snapshot.id,
                requested_url=requested_url,
                final_url=final_url,
                redirect_chain=tuple(redirect_chain),
                attempted_at=now - timedelta(milliseconds=max(duration_ms or 0, 0)),
                completed_at=now,
                http_status=http_status,
                declared_content_type=(declared_content_type or "").split(";", 1)[0] or None,
                detected_content_type=detected_content_type,
                encoded_size=encoded_size,
                encoded_sha256=encoded_sha256,
                decoded_size=decoded_size,
                decoded_sha256=decoded_sha256,
                content_encoding=None,
                allowed_headers=allowed_headers or {},
                outcome=AttemptOutcome.ERROR,
                failure_reason=reason,
                reason_code=reason_code,
                candidate_resolution_url=_same_site_redirect_candidate(
                    requested_url, final_url, redirect_chain
                ),
                candidate_resolution_provenance=(
                    CollectionResolutionProvenance.SAME_SITE_HTTP_REDIRECT
                    if _same_site_redirect_candidate(requested_url, final_url, redirect_chain)
                    else None
                ),
                strategy_name=strategy_name,
                strategy_version=strategy_version,
                duration_ms=duration_ms,
            )
            await uow.collection_attempts.append(attempt)
            collection.record_fallback_attempt(attempt.id)
            await uow.source_collections.save(collection)
            await uow.provenance.append(
                _fallback_attempt_event(
                    collection,
                    max(
                        (subject.tlp, collection.source_tlp),
                        key=lambda item: tuple(TLP).index(item),
                    ),
                    method=method,
                    requested_url=requested_url,
                    outcome=outcome,
                    attempt_id=attempt.id,
                    reason_code=reason_code.value,
                    model_run_id=model_run_id,
                    consultation_id=consultation_id,
                    prompt_sha256=prompt_sha256,
                    response_sha256=response_sha256,
                    strategy_name=strategy_name,
                    strategy_version=strategy_version,
                    duration_ms=duration_ms,
                    http_status=http_status,
                    byte_count=encoded_size,
                    sha256=encoded_sha256,
                )
            )
            await uow.commit()

    async def _record_fallback_probe(
        self,
        collection_id: UUID,
        job_id: UUID,
        probe: AcquisitionProbe,
    ) -> None:
        """Persist one strategy attempt in both existing audit repositories."""
        now = datetime.now(UTC)
        response = probe.response
        success = (
            probe.outcome
            in {
                AcquisitionOutcome.SUCCEEDED,
                AcquisitionOutcome.LOOKUP_SUCCEEDED,
                AcquisitionOutcome.METADATA_SUCCEEDED,
            }
            and response is not None
            and probe.policy_decision is AcquisitionPolicyDecision.ALLOWED
        )
        reason = probe.reason_code or probe.outcome
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            subject = await uow.subjects.get(collection.subject_id)
            if (
                subject is None
                or collection.state in (_COLLECTED_STATES | {CollectionState.FETCHING})
                or collection.source_document_id is not None
            ):
                return
            await uow.collection_policy_snapshots.add_if_absent(self._policy_snapshot)
            attempt = CollectionAttempt(
                collection_id=collection.id,
                job_id=job_id,
                manual_lease_id=None,
                policy_snapshot_id=self._policy_snapshot.id,
                requested_url=safe_fallback_url(probe.requested_url),
                final_url=(safe_fallback_url(response.final_url) if response is not None else None),
                redirect_chain=(
                    tuple(safe_fallback_url(item) for item in response.redirect_chain)
                    if response is not None
                    else ()
                ),
                attempted_at=now - timedelta(milliseconds=max(0, probe.duration_ms)),
                completed_at=now,
                http_status=probe.http_status,
                declared_content_type=(
                    response.declared_content_type if response is not None else None
                ),
                detected_content_type=(
                    response.detected_content_type.value if response is not None else None
                ),
                encoded_size=(
                    response.encoded_size if response is not None else probe.encoded_size
                ),
                encoded_sha256=(
                    response.encoded_sha256 if response is not None else probe.encoded_sha256
                ),
                decoded_size=response.decoded_size if response is not None else None,
                decoded_sha256=response.decoded_sha256 if response is not None else None,
                content_encoding=response.content_encoding if response is not None else None,
                allowed_headers=response.headers if response is not None else {},
                outcome=(
                    AttemptOutcome.SUCCEEDED
                    if success
                    else AttemptOutcome.SKIPPED
                    if probe.outcome
                    in {
                        AcquisitionOutcome.SKIPPED,
                        AcquisitionOutcome.NOT_CONFIGURED,
                        AcquisitionOutcome.POLICY_SKIPPED,
                    }
                    else AttemptOutcome.ERROR
                ),
                failure_reason=None if success else f"Acquisition strategy {reason}",
                reason_code=(
                    None
                    if success
                    else CollectionFailureReason.BRIDGE_TRANSCRIPTION_FAILED
                    if probe.method is SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
                    else CollectionFailureReason.FALLBACK_FAILED
                ),
                strategy_name=probe.strategy_name,
                strategy_version=probe.strategy_version,
                duration_ms=probe.duration_ms,
                acquisition_method=probe.method,
                source_equivalence_class=probe.equivalence_class,
                evidence_class=probe.evidence_class,
                observation_kind=probe.observation_kind,
                quality_score=probe.quality.score if probe.quality is not None else None,
                quality_assessment=_quality_assessment(probe.quality),
                archive_snapshot_time=probe.archive_snapshot_time,
                model_run_id=UUID(probe.model_run_id) if probe.model_run_id else None,
                model_generated=probe.model_generated,
                primary_source_verified=probe.primary_source_verified,
            )
            await uow.collection_attempts.append(attempt)
            collection.record_fallback_attempt(attempt.id)
            await uow.source_collections.save(collection)
            await uow.provenance.append(
                _fallback_attempt_event(
                    collection,
                    max(
                        (subject.tlp, collection.source_tlp),
                        key=lambda item: tuple(TLP).index(item),
                    ),
                    method=probe.method,
                    requested_url=probe.requested_url,
                    outcome=probe.outcome,
                    attempt_id=attempt.id,
                    reason_code=probe.reason_code,
                    strategy_name=probe.strategy_name,
                    strategy_version=probe.strategy_version,
                    duration_ms=probe.duration_ms,
                    http_status=probe.http_status,
                    byte_count=probe.encoded_size,
                    sha256=probe.encoded_sha256,
                    model_run_id=UUID(probe.model_run_id) if probe.model_run_id else None,
                    consultation_id=probe.consultation_id,
                    prompt_sha256=probe.prompt_sha256,
                    response_sha256=probe.response_sha256,
                )
            )
            await uow.commit()

    async def _record_fallback_skip(
        self,
        collection: SourceCollection,
        *,
        job_id: UUID,
        method: SourceAcquisitionMethod,
        reason_code: str,
        outcome: AcquisitionOutcome | str = AcquisitionOutcome.SKIPPED,
        requested_url: str | None = None,
        model_run_id: UUID | None = None,
        consultation_id: str | None = None,
        prompt_sha256: str | None = None,
        response_sha256: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        await self._record_fallback_probe(
            collection.id,
            job_id,
            AcquisitionProbe(
                strategy_name=method.value,
                strategy_version="1",
                method=method,
                requested_url=requested_url or collection.canonical_url,
                outcome=_typed_probe_outcome(outcome),
                reason_code=reason_code,
                response=None,
                http_status=None,
                encoded_size=None,
                encoded_sha256=None,
                duration_ms=duration_ms,
                model_run_id=str(model_run_id) if model_run_id else None,
                consultation_id=consultation_id,
                prompt_sha256=prompt_sha256,
                response_sha256=response_sha256,
            ),
        )

    async def _fallback_diagnostic(
        self,
        context: JobExecutionContext | None,
        collection: SourceCollection,
        *,
        method: SourceAcquisitionMethod,
        state: str,
        reason_code: str | None,
        requested_url: str,
        strategy_name: str | None = None,
        strategy_version: str | None = None,
        duration_ms: int | None = None,
        http_status: int | None = None,
        byte_count: int | None = None,
        sha256: str | None = None,
    ) -> None:
        if context is None:
            return
        safe_url = safe_fallback_url(requested_url)
        await context.record_diagnostics(
            {
                "source_collection_id": str(collection.id),
                "acquisition_method": method.value,
                "strategy_name": strategy_name or method.value,
                "strategy_version": strategy_version or "1",
                "state": state,
                "reason_code": reason_code,
                "url": safe_url,
                "http_status": http_status,
                "bytes": byte_count,
                "sha256": sha256,
                "duration_ms": duration_ms,
                "correlation_id": get_correlation_id(),
            }
        )

    @staticmethod
    def _bridge_fallback_status(events: Sequence[ProvenanceEvent]) -> str | None:
        if not events:
            return None
        event = max(events, key=lambda item: item.occurred_at)
        payload = event.payload if isinstance(event.payload, dict) else {}
        return str(payload.get("outcome") or "failed")

    async def _failure_diagnostics(
        self, sources: Sequence[SourceCollection]
    ) -> list[dict[str, object]]:
        failures: list[dict[str, object]] = []
        async with self._uow_factory() as uow:
            for source in sources:
                current = await uow.source_collections.get(source.id)
                if current is None or current.state in _COLLECTED_STATES:
                    continue
                attempts = await uow.collection_attempts.list_for_collection(source.id)
                latest = attempts[-1] if attempts else None
                failures.append(
                    {
                        "collection_id": str(source.id),
                        "requested_url": safe_fallback_url(
                            latest.requested_url if latest else source.requested_url
                        ),
                        "state": current.state.value,
                        "reason_code": (
                            current.failure_reason_code.value
                            if current.failure_reason_code is not None
                            else latest.reason_code.value
                            if latest and latest.reason_code is not None
                            else None
                        ),
                        "failure_reason": current.error_reason,
                        "http_status": latest.http_status if latest else None,
                        "final_url": safe_fallback_url(latest.final_url)
                        if latest and latest.final_url
                        else None,
                        "redirect_chain": [safe_fallback_url(url) for url in latest.redirect_chain]
                        if latest
                        else [],
                        "declared_content_type": (latest.declared_content_type if latest else None),
                        "detected_content_type": (latest.detected_content_type if latest else None),
                        "encoded_size": latest.encoded_size if latest else None,
                        "transport_classification": (
                            latest.transport_classification.value
                            if latest and latest.transport_classification is not None
                            else None
                        ),
                        "candidate_resolution_url": (
                            latest.candidate_resolution_url if latest else None
                        ),
                        "candidate_resolution_provenance": (
                            latest.candidate_resolution_provenance if latest else None
                        ),
                    }
                )
        return failures


def register_collection_jobs(registry: JobRegistry, service: SubjectCollectionService) -> None:
    async def handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, SubjectCollectionParameters):
            raise TypeError("Invalid source collection parameters")
        return await service.collect_subject(
            parameters.subject_id,
            context.job_id,
            context,
            collection_id=parameters.collection_id,
        )

    registry.register("source.collect", SubjectCollectionParameters, handler)

    async def fallback_handler(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, FallbackAcquireParameters):
            raise TypeError("Invalid source fallback acquisition parameters")
        state = await service.fallback_acquire(
            parameters.subject_id,
            parameters.collection_id,
            context.job_id,
            context,
        )
        return state.value

    registry.register("source.fallback_acquire", FallbackAcquireParameters, fallback_handler)


def collection_idempotency_key(
    subject_id: UUID,
    policy_snapshot_id: str,
    round_number: int,
    *,
    collection_id: UUID | None = None,
) -> str:
    target = str(collection_id) if collection_id else "all"
    return f"source.collect:{subject_id}:{target}:{policy_snapshot_id}:{round_number}"


def _new_collection(
    edition_id: UUID,
    subject_id: UUID,
    candidate: DiscoveryCandidate,
    source: SourceCandidate,
) -> SourceCollection:
    stored_url, credential_like = _safe_collection_url(source.canonical_url)
    return SourceCollection(
        subject_id=subject_id,
        edition_id=edition_id,
        batch_id=candidate.discovery_batch_id,
        source_candidate_id=source.id,
        requested_url=stored_url,
        canonical_url=stored_url,
        origin_kind=SourceOriginKind.DISCOVERY,
        proposed_role=source.role,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        source_tlp=source.tlp,
        sensitivity=source.sensitivity,
        external_llm_allowed=source.external_llm_allowed,
        error_reason=_CREDENTIAL_LIKE_URL_REJECTION if credential_like else None,
    )


def _new_snapshot_collection(
    snapshot: ProductionInputSnapshot,
    subject_id: UUID,
    source: ProductionInputSource,
) -> SourceCollection:
    stored_url, credential_like = _safe_collection_url(source.canonical_url)
    return SourceCollection(
        subject_id=subject_id,
        edition_id=snapshot.edition_id,
        batch_id=source.discovery_batch_id,
        source_candidate_id=source.source_candidate_id,
        requested_url=stored_url,
        canonical_url=stored_url,
        origin_kind=SourceOriginKind.DISCOVERY,
        proposed_role=source.role,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        source_tlp=source.tlp,
        sensitivity=source.sensitivity,
        external_llm_allowed=source.external_llm_allowed,
        error_reason=_CREDENTIAL_LIKE_URL_REJECTION if credential_like else None,
    )


def _safe_collection_url(url: str) -> tuple[str, bool]:
    """Store only a safe origin when the source URL contains credentials."""
    if is_credential_free_url(url):
        return url, False
    return credential_free_url(url), True


def _successful_attempt(
    collection: SourceCollection,
    started_at: datetime,
    response: CollectedResponse,
    policy_snapshot_id: str,
    *,
    job_id: UUID | None,
    manual_lease_id: UUID | None,
    acquisition_method: SourceAcquisitionMethod = SourceAcquisitionMethod.DIRECT_HTTP,
    processing_failure: tuple[CollectionFailureReason, str] | None = None,
    strategy_name: str | None = None,
    strategy_version: str | None = None,
    duration_ms: int | None = None,
    fallback: _FallbackAcquisition | None = None,
) -> CollectionAttempt:
    failure_reason_code = processing_failure[0] if processing_failure else None
    failure_reason = processing_failure[1] if processing_failure else None
    outcome = (
        AttemptOutcome.BLOCKED
        if failure_reason_code is CollectionFailureReason.ACCESS_BLOCKED
        else AttemptOutcome.ERROR
        if processing_failure is not None
        else AttemptOutcome.SUCCEEDED
    )
    resolution_url = _same_site_redirect_candidate(
        response.requested_url, response.final_url, response.redirect_chain
    )
    return CollectionAttempt(
        collection_id=collection.id,
        job_id=job_id,
        manual_lease_id=manual_lease_id,
        policy_snapshot_id=policy_snapshot_id,
        requested_url=response.requested_url,
        final_url=response.final_url,
        redirect_chain=response.redirect_chain,
        attempted_at=started_at,
        completed_at=datetime.now(UTC),
        http_status=(
            None
            if acquisition_method is SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
            else response.status
        ),
        declared_content_type=response.declared_content_type,
        detected_content_type=response.detected_content_type.value,
        encoded_size=response.encoded_size,
        encoded_sha256=response.encoded_sha256,
        decoded_size=response.decoded_size,
        decoded_sha256=response.decoded_sha256,
        content_encoding=response.content_encoding,
        allowed_headers=response.headers,
        outcome=outcome,
        failure_reason=failure_reason,
        reason_code=failure_reason_code,
        candidate_resolution_url=resolution_url,
        candidate_resolution_provenance=(
            CollectionResolutionProvenance.SAME_SITE_HTTP_REDIRECT
            if resolution_url is not None
            else None
        ),
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        duration_ms=duration_ms,
        acquisition_method=acquisition_method,
        source_equivalence_class=(
            fallback.source_equivalence_class if fallback is not None else None
        ),
        evidence_class=fallback.evidence_class if fallback is not None else None,
        observation_kind=fallback.observation_kind if fallback is not None else None,
        quality_score=(
            fallback.quality.score if fallback is not None and fallback.quality else None
        ),
        quality_assessment=(
            _quality_assessment(fallback.quality) if fallback is not None else None
        ),
        archive_snapshot_time=(fallback.archive_snapshot_time if fallback is not None else None),
        model_run_id=fallback.model_run_id if fallback is not None else None,
        model_generated=fallback.model_generated if fallback is not None else None,
        primary_source_verified=(
            fallback.primary_source_verified if fallback is not None else None
        ),
    )


def _quality_assessment(quality: AcquisitionQuality | None) -> dict[str, object] | None:
    if quality is None:
        return None
    return {"score": quality.score, "reasons": list(quality.reasons)}


def _fallback_attempt_event(
    collection: SourceCollection,
    tlp: TLP,
    *,
    method: SourceAcquisitionMethod,
    requested_url: str,
    outcome: str,
    attempt_id: UUID | None,
    reason_code: str | None,
    model_run_id: UUID | None = None,
    consultation_id: str | None = None,
    prompt_sha256: str | None = None,
    response_sha256: str | None = None,
    strategy_name: str | None = None,
    strategy_version: str = "1",
    duration_ms: int | None = None,
    http_status: int | None = None,
    byte_count: int | None = None,
    sha256: str | None = None,
) -> ProvenanceEvent:
    payload: dict[str, object] = {
        "acquisition_method": method.value,
        "strategy_name": strategy_name or method.value,
        "strategy_version": strategy_version,
        "outcome": outcome,
        "reason_code": reason_code,
        "attempt_id": str(attempt_id) if attempt_id is not None else None,
        "attempted_url_sha256": hashlib.sha256(requested_url.encode("utf-8")).hexdigest(),
        "attempted_strategy_url_sha256": hashlib.sha256(
            f"{strategy_name or method.value}\0{requested_url}".encode()
        ).hexdigest(),
        "duration_ms": duration_ms,
        "http_status": http_status,
        "bytes": byte_count,
        "sha256": sha256,
    }
    for key, value in (
        ("model_run_id", str(model_run_id) if model_run_id is not None else None),
        ("consultation_id", consultation_id),
        ("prompt_sha256", prompt_sha256),
        ("response_sha256", response_sha256),
    ):
        if value is not None:
            payload[key] = value
    return ProvenanceEvent(
        subject_id=collection.subject_id,
        aggregate_type="source_collection",
        aggregate_id=collection.id,
        event_type="source.acquisition_fallback_attempted",
        payload=payload,
        tlp=tlp,
        actor_id="system:acquisition_fallback",
    )


def _interrupted_attempt(
    collection: SourceCollection,
    completed_at: datetime,
    *,
    fallback_job_id: UUID,
    fallback_policy_snapshot_id: str,
    reason: str = "Previous fetch lease expired before archival",
) -> CollectionAttempt:
    # An abandoned analyst upload is recorded under its own lease, not
    # attributed to the collector job that happens to take the source over.
    manual_lease_id = collection.manual_lease_id if collection.fetch_job_id is None else None
    job_id = None if manual_lease_id else (collection.fetch_job_id or fallback_job_id)
    return CollectionAttempt(
        collection_id=collection.id,
        job_id=job_id,
        manual_lease_id=manual_lease_id,
        policy_snapshot_id=(collection.fetch_policy_snapshot_id or fallback_policy_snapshot_id),
        requested_url=collection.requested_url,
        final_url=None,
        redirect_chain=(),
        attempted_at=collection.fetch_started_at or completed_at,
        completed_at=completed_at,
        http_status=None,
        declared_content_type=None,
        detected_content_type=None,
        encoded_size=None,
        encoded_sha256=None,
        decoded_size=None,
        decoded_sha256=None,
        content_encoding=None,
        allowed_headers={},
        outcome=AttemptOutcome.INTERRUPTED,
        failure_reason=reason,
        reason_code=CollectionFailureReason.INTERRUPTED,
    )


def _same_site_redirect_candidate(
    requested_url: str,
    final_url: str | None,
    redirect_chain: Sequence[str],
) -> str | None:
    if not final_url or not redirect_chain:
        return None
    try:
        requested = urlsplit(requested_url)
        final = urlsplit(final_url)
    except ValueError:
        return None
    if (
        requested.hostname is None
        or final.hostname is None
        or not _same_site_host(requested.hostname, final.hostname)
        or final.scheme.casefold() != "https"
        or final.port not in {None, 443}
        or final_url == requested_url
    ):
        return None
    return final_url


def _same_site_host(first: str, second: str) -> bool:
    first_host = first.casefold().rstrip(".")
    second_host = second.casefold().rstrip(".")
    return (
        first_host == second_host
        or first_host.endswith(f".{second_host}")
        or second_host.endswith(f".{first_host}")
    )


def _pdf_processing_failure(
    requested_url: str, response: CollectedResponse
) -> tuple[CollectionFailureReason, str] | None:
    path = urlsplit(requested_url).path.casefold()
    declared_as_pdf = response.declared_content_type == DetectedMimeType.PDF.value
    expects_pdf = path.endswith(".pdf") or declared_as_pdf
    if not expects_pdf:
        return None
    if response.detected_content_type is not DetectedMimeType.PDF:
        preview = response.decoded_body[: 128 * 1024].decode("utf-8", errors="ignore").casefold()
        challenge_markers = (
            "captcha",
            "cloudflare challenge",
            "challenge-platform",
            "verify you are human",
            "checking your browser",
        )
        if response.detected_content_type is DetectedMimeType.HTML and any(
            marker in preview for marker in challenge_markers
        ):
            return (
                CollectionFailureReason.ACCESS_BLOCKED,
                "Access denied by a bot-protection challenge instead of a PDF",
            )
        return (
            CollectionFailureReason.NON_PDF_CONTENT,
            "The requested PDF URL served "
            f"{response.detected_content_type.value} (declared "
            f"{response.declared_content_type or 'unknown'})",
        )
    try:
        parse_document(response.decoded_body, DetectedMimeType.PDF)
    except DocumentParsingError as exc:
        if exc.code == "pdf_timeout":
            return (CollectionFailureReason.TIMEOUT, str(exc))
        if exc.code in {
            "pdf_too_large",
            "pdf_too_many_pages",
            "pdf_text_too_large",
            "pdf_metadata_too_large",
        }:
            return (CollectionFailureReason.SIZE_LIMIT, str(exc))
        return (CollectionFailureReason.PDF_PARSE_FAILURE, str(exc))
    return None


def _processing_failure_state(
    failure: tuple[CollectionFailureReason, str] | None,
) -> CollectionState:
    if failure is not None and failure[0] is CollectionFailureReason.ACCESS_BLOCKED:
        return CollectionState.BLOCKED
    return CollectionState.FAILED_TERMINAL


async def _discovery_source(
    uow: UnitOfWork, batch_id: UUID | None, source_candidate_id: UUID | None
) -> tuple[SourceCandidate | None, list[DiscoveryCandidate]]:
    """Resolve a discovery publication from the persisted candidates of a batch.

    A collection still records the batch it came from as provenance, but the
    publication itself is read from `discovery_candidates` — the canonical
    store — and never from the batch payload.
    """
    if batch_id is None or source_candidate_id is None:
        return None, []
    owners: list[DiscoveryCandidate] = []
    found: SourceCandidate | None = None
    for candidate in await uow.discovery_candidates.list_for_batch(batch_id):
        match = next(
            (item for item in candidate.evidence.sources if item.id == source_candidate_id), None
        )
        if match is None:
            continue
        owners.append(candidate)
        found = found or match
    return found, owners


async def _require_collection(uow: UnitOfWork, collection_id: UUID) -> SourceCollection:
    # PostgreSQL holds this row lock through the caller's commit, which keeps
    # collection pointer and lease precedence checks atomic for fallback saves.
    collection = await uow.source_collections.get_for_update(collection_id)
    if collection is None:
        raise CollectionItemNotFoundError(str(collection_id))
    return collection


def _original_name(url: str) -> str:
    name = PurePosixPath(url.split("?", 1)[0]).name
    return name[:500] if name else "source"


def _state_label(state: CollectionState) -> str:
    return {
        CollectionState.ARCHIVED: "archivée",
        CollectionState.EXTRACTED: "déjà archivée",
        CollectionState.COMPLETED: "déjà archivée",
        CollectionState.UNAVAILABLE: "indisponible",
        CollectionState.BLOCKED: "bloquée",
        CollectionState.FAILED_RETRYABLE: "à réessayer",
        CollectionState.FAILED_TERMINAL: "en échec définitif",
        CollectionState.FETCHING: "déjà en cours",
    }.get(state, state.value)


def _summary_message(summary: CollectionSummary) -> str:
    message = (
        f"{summary.total} publications traitées · "
        f"{summary.already_archived} déjà archivées · "
        f"{summary.newly_archived} nouvellement archivées"
    )
    if summary.warning_count:
        message += f" · {summary.warning_count} avertissements"
    return message

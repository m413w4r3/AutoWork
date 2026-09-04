from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.collection_errors import (
    CollectionItemNotFoundError,
    CollectionNotAllowedError,
)
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionError,
    DownloadTooLargeError,
    SafeHttpCollector,
    UnsupportedContentError,
    _detect_mime,
)
from cti_app.application.jobs import (
    JobCancelledError,
    JobExecutionContext,
    JobHandlerError,
    JobParameters,
    JobRegistry,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.source_filenames import analyst_filename
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.domain.collection import (
    AttemptOutcome,
    CollectionAttempt,
    CollectionPolicySnapshot,
    CollectionState,
    DetectedMimeType,
    SourceCollection,
    SourceOriginKind,
)
from cti_app.domain.discovery import SourceCandidate, SourceRole, canonicalize_http_url
from cti_app.domain.editorial import EditorialGroupStatus
from cti_app.domain.entities import ProvenanceEvent, SourceDocument
from cti_app.domain.production import ProductionInputSnapshot, ProductionInputSource
from cti_app.logging import get_correlation_id

logger = logging.getLogger(__name__)
_COLLECTED_STATES = {
    CollectionState.ARCHIVED,
    CollectionState.EXTRACTED,
    CollectionState.COMPLETED,
}


def _snapshot_source_urls(snapshot: ProductionInputSnapshot) -> frozenset[str]:
    """Return the frozen source URL set in deterministic order-independent form."""
    return frozenset(source.canonical_url for source in snapshot.core_sources)


class ManualContentTypeError(CollectionNotAllowedError):
    """The supplied bytes do not contain a supported source document."""


class ManualContentEmptyError(CollectionNotAllowedError):
    """The supplied source document has no content."""


class ManualContentTooLargeError(CollectionNotAllowedError):
    """The supplied source document exceeds the collection policy limit."""


class ManualContentAlreadyArchivedError(CollectionNotAllowedError):
    """The immutable archived source cannot be replaced."""


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
    ) -> None:
        self._uow_factory = uow_factory
        self._collector = collector
        self._blob_store = blob_store
        self._catalog = BlobCatalogService(blob_store, uow_factory)
        self._policy = self._collector.policy
        self._workspace_materializer = workspace_materializer
        self._workspace_root = workspace_root
        self._fetch_lease = timedelta(
            seconds=max(fetch_lease_seconds, self._collector.policy.timeout_seconds + 5)
        )
        self._policy_snapshot = self._collector.policy.snapshot()

    async def read_blob(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        """Read archived content back, for consumers holding only a blob id."""
        return await self._catalog.read(blob_id, max_bytes=max_bytes)

    async def initialize(self, subject_id: UUID) -> list[SourceCollection]:
        async with self._uow_factory() as uow:
            group = await uow.editorial_groups.get_by_subject(subject_id)
            if (
                group is None
                or group.status is not EditorialGroupStatus.SELECTED
                or group.subject_id != subject_id
            ):
                raise CollectionNotAllowedError(
                    "Only sources attached to a selected subject can be collected"
                )
            batches = {
                batch.id: batch
                for batch in await uow.discovery_batches.list_for_edition(group.edition_id)
            }
            # Une nouvelle contribution peut réintroduire une URL déjà rattachée au
            # sujet sous un SourceCandidate.id différent. La clé d'unicité en base
            # étant (subject_id, source_candidate_id), il faut dédupliquer sur
            # l'URL canonique face à ce qui est déjà collecté ou archivé (§28).
            seen_urls: set[str] = {
                canonicalize_http_url(collection.requested_url)
                for collection in await uow.source_collections.list_for_subject(subject_id)
            }
            seen_urls.update(
                canonicalize_http_url(document.origin)
                for document in await uow.source_documents.list_for_subject(subject_id)
                if document.origin
            )
            for reference in group.candidate_references:
                batch = batches.get(reference.batch_id)
                candidate = (
                    next(
                        (item for item in batch.candidates if item.id == reference.candidate_id),
                        None,
                    )
                    if batch
                    else None
                )
                if candidate is None:
                    continue
                for source in candidate.sources:
                    if source.canonical_url in seen_urls:
                        continue
                    seen_urls.add(source.canonical_url)
                    await uow.source_collections.add_if_absent(
                        _new_collection(
                            group.id,
                            group.edition_id,
                            subject_id,
                            reference.batch_id,
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
                if source.canonical_url in seen_urls:
                    continue
                await uow.source_collections.add_if_absent(
                    _new_snapshot_collection(snapshot, subject_id, source)
                )
                seen_urls.add(source.canonical_url)
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
            group = await uow.editorial_groups.get_by_subject(subject_id)
            if group is None or group.status is not EditorialGroupStatus.SELECTED:
                raise CollectionNotAllowedError(
                    "Only sources attached to a selected subject can be collected"
                )
            added: list[SourceCollection] = []
            for candidate in sources:
                try:
                    canonical = canonicalize_http_url(candidate.url)
                except ValueError:
                    continue
                existing = await uow.source_collections.get_by_canonical_url(subject_id, canonical)
                if existing is not None:
                    continue
                collection = SourceCollection(
                    subject_id=subject_id,
                    edition_id=group.edition_id,
                    group_id=group.id,
                    requested_url=canonical,
                    canonical_url=canonical,
                    origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
                    proposed_role=candidate.role,
                    title=candidate.title,
                    publisher=candidate.publisher,
                    published_at=candidate.published_at,
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

        async with self._uow_factory() as uow:
            existing = await uow.source_collections.get_by_canonical_url(
                subject_id, canonical
            )
        if existing is not None:
            return existing

        await self.add_supplemental_sources(
            subject_id, [replace(source, url=canonical)]
        )

        async with self._uow_factory() as uow:
            created = await uow.source_collections.get_by_canonical_url(
                subject_id, canonical
            )
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
                if await uow.source_collections.get_by_canonical_url(subject_id, canonical):
                    continue
                child = SourceCollection(
                    subject_id=parent.subject_id,
                    edition_id=parent.edition_id,
                    group_id=parent.group_id,
                    requested_url=canonical,
                    canonical_url=canonical,
                    origin_kind=SourceOriginKind.REFERENCED_EVIDENCE,
                    parent_source_collection_id=parent.id,
                    proposed_role=SourceRole.UNKNOWN,
                    title=resource.anchor_text or None,
                    source_tlp=parent.source_tlp,
                    sensitivity=parent.sensitivity,
                    external_llm_allowed=parent.external_llm_allowed,
                    do_not_submit=parent.do_not_submit,
                    relationship_evidence="deterministic:referenced_evidence_link",
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

    async def attempts(self, collection_id: UUID) -> list[CollectionAttempt]:
        async with self._uow_factory() as uow:
            if await uow.source_collections.get(collection_id) is None:
                raise CollectionItemNotFoundError(str(collection_id))
            return list(await uow.collection_attempts.list_for_collection(collection_id))

    async def source_context(
        self, source: SourceCollection
    ) -> tuple[SourceCandidate | None, SourceDocument | None]:
        async with self._uow_factory() as uow:
            candidate = None
            if source.batch_id is not None and source.source_candidate_id is not None:
                batch = await uow.discovery_batches.get(source.batch_id)
                candidate = batch.source(source.source_candidate_id) if batch else None
            document = (
                await uow.source_documents.get(source.source_document_id)
                if source.source_document_id
                else None
            )
            return candidate, document

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
                    "url": source.requested_url,
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
                    "url": source.requested_url,
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
                details=asdict(summary),
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
        await self._archive(collection.id, job_id, started_at, response, candidate=candidate)
        self._log_source_result(
            collection,
            job_id,
            CollectionState.ARCHIVED,
            operation_started,
            size=response.decoded_size,
        )
        return CollectionState.ARCHIVED

    async def archive_manual_content(
        self,
        collection_id: UUID,
        *,
        content: bytes,
        declared_mime_type: str,
        final_url: str | None = None,
        actor_id: str,
    ) -> SourceCollection:
        """Archive analyst-supplied bytes as if they had been fetched.

        An anti-bot page, a 403 or a JavaScript-rendered article cannot be
        collected, yet the analyst holds the exact publication. The evidence
        gate only ever compares proposals to the archived text, so supplying
        that text restores the whole downstream chain — extraction, evidence
        verification, publication — with no special case anywhere else.
        """
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

        job_id = uuid4()
        started_at = datetime.now(UTC)
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            if collection.state in _COLLECTED_STATES:
                raise ManualContentAlreadyArchivedError("source_already_archived")
            claimed = collection.claim_manual_upload(
                job_id,
                lease_duration=self._fetch_lease,
                policy_snapshot_id=self._policy_snapshot.id,
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
        await self._archive(collection_id, job_id, started_at, response)

        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            subject = await uow.subjects.get(collection.subject_id)
            if subject is None:
                raise CollectionNotAllowedError("Collection source lost its canonical context")
            if collection.origin_kind is not SourceOriginKind.MANUAL:
                collection.origin_kind = SourceOriginKind.MANUAL
                await uow.source_collections.save(collection)
            await uow.provenance.append(
                ProvenanceEvent(
                    subject_id=collection.subject_id,
                    aggregate_type="source_collection",
                    aggregate_id=collection.id,
                    event_type="source.archived_manually",
                    payload={
                        "actor_id": actor_id,
                        "declared_mime_type": declared_mime_type,
                        "size": response.decoded_size,
                        "decoded_sha256": response.decoded_sha256,
                    },
                    tlp=subject.tlp,
                    actor_id=actor_id,
                )
            )
            await uow.commit()
            return collection

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
        if collection.batch_id is None or collection.source_candidate_id is None:
            return None
        async with self._uow_factory() as uow:
            batch = await uow.discovery_batches.get(collection.batch_id)
            return batch.source(collection.source_candidate_id) if batch else None

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
        logger.info(
            "source_collection_result",
            extra={
                "event": "source.collection.persisted",
                "job_id": str(job_id),
                "subject_id": str(collection.subject_id),
                "source_collection_id": str(collection.id),
                "source_candidate_id": str(collection.source_candidate_id),
                "requested_url": collection.requested_url,
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
        job_id: UUID,
        started_at: datetime,
        response: CollectedResponse,
        *,
        candidate: SourceCandidate | None = None,
    ) -> None:
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
            if (
                source is None
                and collection.batch_id is not None
                and collection.source_candidate_id is not None
            ):
                batch = await uow.discovery_batches.get(collection.batch_id)
                source = batch.source(collection.source_candidate_id) if batch else None
            if subject is None:
                raise CollectionNotAllowedError("Collection source lost its canonical context")
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
            meta_tlp = source.tlp if source else collection.source_tlp
            meta_external_allowed = (
                source.external_llm_allowed if source else collection.external_llm_allowed
            )
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
                origin=response.requested_url,
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
                job_id,
                started_at,
                response,
                self._policy_snapshot.id,
            )
            await uow.source_documents.add(document)
            await uow.collection_attempts.append(attempt)
            collection.archive(
                job_id=job_id,
                attempt_id=attempt.id,
                source_document_id=document.id,
                decoded_blob_id=decoded_blob.id,
            )
            await uow.source_collections.save(collection)
            await uow.provenance.append(
                ProvenanceEvent(
                    subject_id=collection.subject_id,
                    aggregate_type="source_collection",
                    aggregate_id=collection.id,
                    event_type="source.archived",
                    payload={
                        "attempt_id": str(attempt.id),
                        "source_document_id": str(document.id),
                        "encoded_sha256": response.encoded_sha256,
                        "decoded_sha256": response.decoded_sha256,
                        "logical_filename": logical_filename,
                        "raw_blob_id": str(raw_blob.id),
                        "decoded_blob_id": str(decoded_blob.id),
                        "content_encoding": response.content_encoding,
                        "job_id": str(job_id),
                    },
                    tlp=subject.tlp,
                    actor_id="system:collector",
                )
            )
            await uow.commit()

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
            )
            await uow.source_collections.save(collection)
            await uow.commit()

    async def _record_failure(
        self,
        collection_id: UUID,
        job_id: UUID,
        started_at: datetime,
        error: CollectionError,
    ) -> CollectionState:
        completed_at = datetime.now(UTC)
        async with self._uow_factory() as uow:
            collection = await _require_collection(uow, collection_id)
            attempt = CollectionAttempt(
                collection_id=collection.id,
                job_id=job_id,
                policy_snapshot_id=self._policy_snapshot.id,
                requested_url=collection.requested_url,
                final_url=error.final_url,
                redirect_chain=error.redirect_chain,
                attempted_at=started_at,
                completed_at=completed_at,
                http_status=error.http_status,
                declared_content_type=(
                    error.headers.get("content-type", "").split(";", 1)[0] or None
                ),
                detected_content_type=None,
                encoded_size=error.encoded_size,
                encoded_sha256=None,
                decoded_size=None,
                decoded_sha256=None,
                content_encoding=None,
                allowed_headers=error.headers,
                outcome=error.outcome,
                failure_reason=str(error),
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
            collection.fail(state, attempt_id=attempt.id, reason=str(error))
            await uow.source_collections.save(collection)
            await uow.commit()
            return state


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
    group_id: UUID,
    edition_id: UUID,
    subject_id: UUID,
    batch_id: UUID,
    source: SourceCandidate,
) -> SourceCollection:
    return SourceCollection(
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        batch_id=batch_id,
        source_candidate_id=source.id,
        requested_url=source.canonical_url,
        canonical_url=source.canonical_url,
        origin_kind=SourceOriginKind.DISCOVERY,
        proposed_role=source.role,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        source_tlp=source.tlp,
        sensitivity=source.sensitivity,
        external_llm_allowed=source.external_llm_allowed,
    )


def _new_snapshot_collection(
    snapshot: ProductionInputSnapshot,
    subject_id: UUID,
    source: ProductionInputSource,
) -> SourceCollection:
    return SourceCollection(
        subject_id=subject_id,
        edition_id=snapshot.edition_id,
        group_id=snapshot.editorial_group_id,
        batch_id=source.batch_id,
        source_candidate_id=source.source_candidate_id,
        requested_url=source.canonical_url,
        canonical_url=source.canonical_url,
        origin_kind=SourceOriginKind.DISCOVERY,
        proposed_role=source.role,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        source_tlp=source.tlp,
        sensitivity=source.sensitivity,
        external_llm_allowed=source.external_llm_allowed,
    )


def _successful_attempt(
    collection: SourceCollection,
    job_id: UUID,
    started_at: datetime,
    response: CollectedResponse,
    policy_snapshot_id: str,
) -> CollectionAttempt:
    return CollectionAttempt(
        collection_id=collection.id,
        job_id=job_id,
        policy_snapshot_id=policy_snapshot_id,
        requested_url=response.requested_url,
        final_url=response.final_url,
        redirect_chain=response.redirect_chain,
        attempted_at=started_at,
        completed_at=datetime.now(UTC),
        http_status=response.status,
        declared_content_type=response.declared_content_type,
        detected_content_type=response.detected_content_type.value,
        encoded_size=response.encoded_size,
        encoded_sha256=response.encoded_sha256,
        decoded_size=response.decoded_size,
        decoded_sha256=response.decoded_sha256,
        content_encoding=response.content_encoding,
        allowed_headers=response.headers,
        outcome=AttemptOutcome.SUCCEEDED,
        failure_reason=None,
    )


def _interrupted_attempt(
    collection: SourceCollection,
    completed_at: datetime,
    *,
    fallback_job_id: UUID,
    fallback_policy_snapshot_id: str,
    reason: str = "Previous fetch lease expired before archival",
) -> CollectionAttempt:
    return CollectionAttempt(
        collection_id=collection.id,
        job_id=collection.fetch_job_id or fallback_job_id,
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
    )


async def _require_collection(uow: UnitOfWork, collection_id: UUID) -> SourceCollection:
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

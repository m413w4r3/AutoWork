"""Read-only catalogue of locally archived source figures for the editor."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from threading import RLock
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid5

from cti_app.application.persistence import (
    SourceDocumentRepository,
    SourceMediaRepository,
    UnitOfWork,
    UnitOfWorkFactory,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EditorialFigureCatalogEntry,
    build_editorial_figure_catalog,
)
from cti_app.application.production_synthesis import build_synthesis_access_policy
from cti_app.application.source_figure_inventory import (
    MAX_SOURCE_DOCUMENT_BYTES,
    MAX_SOURCE_FIGURE_BYTES,
    MAX_SOURCE_FIGURE_TOTAL_BYTES,
    MAX_SOURCE_FIGURES,
    SourceFigureInventoryResult,
    load_archived_source_figure_inventory,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentV1,
    EditorialFigureDecision,
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    editorial_enrichment_from_json,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_from_json,
)
from cti_app.domain.publication_document import parse_publication_document
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.source_media import SourceMediaRecord

_ACTIVE_SOURCE_STATES = frozenset(
    {CollectionState.ARCHIVED, CollectionState.EXTRACTED, CollectionState.COMPLETED}
)
_OPAQUE_FIGURE_NAMESPACE = uuid5(UUID("0e560cce-ec1b-47a9-90d7-20d69d9d42ea"), "figure-catalog")
_MAX_CONTEXT_CHARS = 1_000
_MAX_LABEL_CHARS = 320
_MAX_CATALOG_SOURCE_DOCUMENTS = 128
_MAX_CATALOG_SOURCE_MEDIA_RECORDS = 512
_MAX_CATALOG_ARCHIVE_BYTES = MAX_SOURCE_FIGURE_TOTAL_BYTES
_CATALOG_CACHE_TTL_SECONDS = 30.0
_CATALOG_CACHE_MAX_ENTRIES = 64
_CATALOG_CACHE_MAX_BYTES = 8 * 1024 * 1024


class EditorialFigureCatalogError(RuntimeError):
    """A safe, stable error from reading the current source-figure catalogue."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FigureEligibility:
    status: str
    selectable: bool
    exclusion_reason: str | None


@dataclass(frozen=True, slots=True)
class FigureCatalogSource:
    source_id: str
    title: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class EditorialFigureCatalogItem:
    """Public catalogue fields plus private, server-resolved archive pointers."""

    figure_id: str
    original_name: str
    original_caption: str | None
    alt_text: str | None
    source: FigureCatalogSource
    page: int | None
    section: str | None
    context_before: str | None
    context_after: str | None
    width: int | None
    height: int | None
    mime_type: str | None
    status: str
    selectable: bool
    exclusion_reason: str | None
    already_used: bool
    _source_figure_id: UUID = field(repr=False)
    _source_document_id: UUID = field(repr=False)
    _blob_id: UUID | None = field(repr=False)
    _sha256: str | None = field(repr=False)
    _byte_size: int | None = field(repr=False)
    _run_id: UUID = field(repr=False)
    _inventory_max_figures: int = field(default=MAX_SOURCE_FIGURES, repr=False)
    _resolved_figure: ResolvedSourceFigureV1 | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ManualSourceFigureArchiveReference:
    """Safe, server-resolved archive facts pinned into a manual figure patch."""

    # ``figure_id`` is the opaque catalogue handle submitted by the client.
    figure_id: UUID
    # This is the canonical inventory identity stored in ResolvedSourceFigureV1.
    source_figure_id: UUID
    production_run_id: UUID
    source_document_id: UUID
    source_url: str
    sha256: str
    mime_type: str
    byte_size: int
    width: int
    height: int
    provenance: str


@dataclass(frozen=True, slots=True)
class EditorialFigureCatalogPage:
    items: tuple[EditorialFigureCatalogItem, ...]
    sources: tuple[FigureCatalogSource, ...]
    total: int
    available_count: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class _CurrentFigureContext:
    subject_id: UUID
    run: ProductionRun
    extraction: Any
    inventory: SourceFigureInventoryResult
    figure_catalog: tuple[EditorialFigureCatalogEntry, ...]
    source_documents: Mapping[UUID, Any]
    public_sources: Mapping[UUID, FigureCatalogSource]
    used_figure_ids: frozenset[UUID]
    used_content: frozenset[tuple[UUID, str, str, int]]
    not_selected_ids: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class _CachedFigureCatalog:
    items: tuple[EditorialFigureCatalogItem, ...]
    access_extraction: ProductionExtractionV1
    effective_tlp: TLP

    @property
    def estimated_bytes(self) -> int:
        size = 512 * len(self.items) + 256 * len(self.access_extraction.sources)
        for item in self.items:
            for value in (
                item.original_name,
                item.original_caption,
                item.alt_text,
                item.source.title,
                item.source.url,
                item.section,
                item.context_before,
                item.context_after,
                item.exclusion_reason,
            ):
                if value is not None:
                    size += len(value.encode("utf-8"))
        for source in self.access_extraction.sources:
            size += len(source.canonical_url.encode("utf-8"))
        return size


class EditorialFigureCatalogCache:
    """Bounded process-local cache for immutable catalogue projections."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: OrderedDict[
            tuple[UUID, UUID, str], tuple[float, _CachedFigureCatalog, int]
        ] = OrderedDict()
        self._total_bytes = 0

    def get(self, key: tuple[UUID, UUID, str]) -> _CachedFigureCatalog | None:
        now = monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value, _weight = entry
            if expires_at <= now:
                self._remove(key)
                return None
            self._entries.move_to_end(key)
            return value

    def put(self, key: tuple[UUID, UUID, str], value: _CachedFigureCatalog) -> None:
        weight = value.estimated_bytes
        if weight <= 0 or weight > _CATALOG_CACHE_MAX_BYTES:
            return
        with self._lock:
            self._remove(key)
            while self._entries and (
                len(self._entries) >= _CATALOG_CACHE_MAX_ENTRIES
                or self._total_bytes + weight > _CATALOG_CACHE_MAX_BYTES
            ):
                self._remove(next(iter(self._entries)))
            self._entries[key] = (
                monotonic() + _CATALOG_CACHE_TTL_SECONDS,
                value,
                weight,
            )
            self._total_bytes += weight

    def _remove(self, key: tuple[UUID, UUID, str]) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._total_bytes -= entry[2]


editorial_figure_catalog_cache = EditorialFigureCatalogCache()


def opaque_figure_id(subject_id: UUID, run_id: UUID, source_figure_id: UUID) -> str:
    """Return a run-scoped handle; storage IDs never participate in the response."""
    namespace = uuid5(_OPAQUE_FIGURE_NAMESPACE, f"{subject_id}:{run_id}")
    return str(uuid5(namespace, f"archived-source-figure:{source_figure_id}"))


_EXCLUSION_MESSAGES = {
    "figure_exceeds_byte_limit": "Le fichier dépasse la taille maximale autorisée.",
    "image_too_large_dimensions": "Les dimensions de cette image dépassent la limite autorisée.",
    "below_minimum_dimensions": "Les dimensions de cette image sont insuffisantes.",
    "unsupported_image_mime_type": "Le format de cette image n'est pas pris en charge.",
    "archived_blob_metadata_mismatch": "Les validations du fichier archivé ne correspondent pas.",
    "duplicate_image": "Cette image est déjà présente dans les sources archivées.",
    "collection_unavailable": "Le fichier source n'a pas pu être vérifié dans l'archive.",
}
_UNAVAILABLE_REASONS = frozenset({"image_not_in_local_archive", "image_bytes_not_in_blob_store"})


def classify_source_figure(
    figure: Any,
    *,
    already_used: bool,
    not_selected: bool,
) -> FigureEligibility:
    """Project inventory decisions to stable UI states without exposing raw text."""
    if figure.decision is SourceFigureDecision.REJECTED:
        return FigureEligibility(
            "ineligible",
            False,
            _EXCLUSION_MESSAGES.get(
                figure.decision_reason,
                "Les validations archivées n'autorisent pas cette figure.",
            ),
        )
    if figure.decision is SourceFigureDecision.PENDING:
        unavailable = figure.decision_reason in _UNAVAILABLE_REASONS or figure.blob_id is None
        return FigureEligibility(
            "file_unavailable" if unavailable else "unverified",
            False,
            (
                "Le fichier image n'est pas disponible dans l'archive."
                if unavailable
                else "Cette figure n'a pas encore été vérifiée."
            ),
        )
    if figure.blob_id is None:
        return FigureEligibility(
            "file_unavailable", False, "Le fichier image n'est pas disponible dans l'archive."
        )
    if already_used:
        return FigureEligibility("already_integrated", False, None)
    if not_selected:
        return FigureEligibility("not_selected", True, None)
    return FigureEligibility("available", True, None)


def archive_reference_for_catalog_item(
    item: EditorialFigureCatalogItem,
) -> ManualSourceFigureArchiveReference:
    """Resolve only an eligible, verified catalogue row to safe archive metadata."""
    figure = item._resolved_figure
    if (
        not item.selectable
        or item.already_used
        or item.status not in {"available", "not_selected"}
        or figure is None
        or figure.decision is not SourceFigureDecision.ACCEPTED
        or figure.blob_id is None
        or figure.sha256 is None
        or figure.mime_type is None
        or figure.byte_size is None
        or item._blob_id != figure.blob_id
        or item._sha256 != figure.sha256
        or item.mime_type != figure.mime_type
        or item._byte_size != figure.byte_size
        or item.width is None
        or item.width < 1
        or item.height is None
        or item.height < 1
    ):
        raise EditorialFigureCatalogError("editorial_figure_not_eligible")
    try:
        figure_id = UUID(item.figure_id)
    except ValueError:
        raise EditorialFigureCatalogError("figure_catalog_unavailable") from None
    return ManualSourceFigureArchiveReference(
        figure_id=figure_id,
        source_figure_id=figure.figure_id,
        production_run_id=item._run_id,
        source_document_id=figure.source_document_id,
        source_url=figure.source,
        sha256=figure.sha256,
        mime_type=figure.mime_type,
        byte_size=figure.byte_size,
        width=item.width,
        height=item.height,
        provenance=figure.provenance,
    )


def assert_archive_reference_matches(
    expected: ManualSourceFigureArchiveReference,
    item: EditorialFigureCatalogItem,
) -> bool:
    """Compare the complete persisted archive fence with a fresh catalogue read."""
    try:
        current = archive_reference_for_catalog_item(item)
    except EditorialFigureCatalogError:
        return False
    return current == expected


def validate_figure_insertion_limit(
    *,
    publication_kind: PublicationKind,
    integrated_count: int,
    requested_count: int,
    inventory_limit: int,
) -> None:
    """Enforce the publication-specific ceiling before preparation and adoption."""
    if (
        type(integrated_count) is not int
        or integrated_count < 0
        or type(requested_count) is not int
        or requested_count < 1
        or type(inventory_limit) is not int
        or inventory_limit < 1
    ):
        raise EditorialFigureCatalogError("figure_catalog_request_invalid")
    if publication_kind is PublicationKind.BRIEF and integrated_count + requested_count > 3:
        raise EditorialFigureCatalogError("brief_figure_limit_exceeded")
    if (
        publication_kind is PublicationKind.ARTICLE
        and integrated_count + requested_count > inventory_limit
    ):
        raise EditorialFigureCatalogError("source_figure_inventory_limit_exceeded")


class EditorialFigureCatalogService:
    """Resolve a catalogue only from the subject's currently pinned production run."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        media_policy_sha256: str | None = None,
        cache: EditorialFigureCatalogCache | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._media_policy_sha256 = media_policy_sha256
        self._cache = cache or editorial_figure_catalog_cache

    async def catalog(
        self,
        *,
        subject_id: UUID,
        status_filter: str | None = None,
        source_filter: UUID | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 30,
    ) -> EditorialFigureCatalogPage:
        if not (0 <= offset <= 512) or not (1 <= limit <= 50):
            raise EditorialFigureCatalogError("figure_catalog_request_invalid")
        items = await self._load_current(subject_id)
        normalized_search = search.strip().casefold() if search else ""
        filtered = tuple(
            item
            for item in items
            if (status_filter is None or item.status == status_filter)
            and (source_filter is None or item.source.source_id == str(source_filter))
            and (
                not normalized_search or normalized_search in self._searchable_text(item).casefold()
            )
        )
        sources_by_id = {item.source.source_id: item.source for item in items}
        sources = tuple(
            sorted(
                sources_by_id.values(), key=lambda item: (item.title or item.source_id).casefold()
            )
        )
        return EditorialFigureCatalogPage(
            items=filtered[offset : offset + limit],
            sources=sources,
            total=len(filtered),
            available_count=sum(1 for item in filtered if item.selectable),
            offset=offset,
            limit=limit,
        )

    async def resolve(self, *, subject_id: UUID, figure_id: UUID) -> EditorialFigureCatalogItem:
        items = await self._load_current(subject_id)
        for item in items:
            if item.figure_id == str(figure_id):
                return item
        raise EditorialFigureCatalogError("editorial_figure_not_found")

    async def source_figure_catalog_ids(
        self, subject_id: UUID
    ) -> tuple[UUID | None, dict[UUID, str]]:
        """Map canonical source figure IDs to current opaque catalogue handles."""
        items = await self._load_current(subject_id)
        if not items:
            return None, {}
        run_id = items[0]._run_id
        return run_id, {
            item._source_figure_id: item.figure_id for item in items if item._run_id == run_id
        }

    async def resolve_for_insertion(
        self, *, subject_id: UUID, figure_id: UUID
    ) -> EditorialFigureCatalogItem:
        """Force an uncached inventory and archived-blob read for a write decision."""
        items = await self.resolve_many_for_insertion(
            subject_id=subject_id, figure_ids=(figure_id,)
        )
        return items[0]

    async def resolve_many_for_insertion(
        self, *, subject_id: UUID, figure_ids: Sequence[UUID]
    ) -> tuple[EditorialFigureCatalogItem, ...]:
        """Resolve a selected set from one fresh inventory and blob verification pass."""
        if not figure_ids or len(set(figure_ids)) != len(figure_ids):
            raise EditorialFigureCatalogError("figure_catalog_request_invalid")
        items = await self._load_current(subject_id, use_cache=False)
        by_id = {item.figure_id: item for item in items}
        selected: list[EditorialFigureCatalogItem] = []
        for figure_id in figure_ids:
            item = by_id.get(str(figure_id))
            if item is None:
                raise EditorialFigureCatalogError("editorial_figure_not_found")
            archive_reference_for_catalog_item(item)
            selected.append(item)
        return tuple(selected)

    async def _load_current(
        self, subject_id: UUID, *, use_cache: bool = True
    ) -> tuple[EditorialFigureCatalogItem, ...]:
        async with self._uow_factory() as uow:
            subject = await uow.subjects.get(subject_id)
            if subject is None:
                raise EditorialFigureCatalogError("editorial_figure_not_found")
            run = await uow.production_runs.get_current_for_subject(subject_id)
            if run is None or run.subject_id != subject_id:
                raise EditorialFigureCatalogError("editorial_figure_not_found")
            production_input = await uow.production_input_snapshots.get_by_run(run.id)
            extraction_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EXTRACTION.value
            )
            if production_input is None or extraction_artifact is None:
                raise EditorialFigureCatalogError("figure_catalog_unavailable")
            if (
                production_input.subject_id != subject_id
                or production_input.production_run_id != run.id
                or extraction_artifact.subject_id != subject_id
                or extraction_artifact.production_run_id != run.id
                or extraction_artifact.stage is not ProductionArtifactStage.EXTRACTION
                or extraction_artifact.status is not ProductionArtifactStatus.VERIFIED
                or extraction_artifact.input_hash != production_input.input_hash
            ):
                raise EditorialFigureCatalogError("figure_catalog_unavailable")

            source_documents = {
                document.id: document
                for document in await uow.source_documents.list_for_subject(subject_id)
                if document.subject_id == subject_id
            }
            if len(source_documents) > _MAX_CATALOG_SOURCE_DOCUMENTS:
                raise EditorialFigureCatalogError("figure_catalog_unavailable")
            collections: dict[UUID, Any] = {}
            active_document_ids: set[UUID] = set()
            for document in source_documents.values():
                collection_id = document.source_collection_id
                if collection_id is None:
                    active_document_ids.add(document.id)
                    continue
                collection = await uow.source_collections.get(collection_id)
                if collection is not None:
                    collections[collection_id] = collection
                if (
                    collection is not None
                    and collection.id == collection_id
                    and collection.subject_id == subject_id
                    and collection.state in _ACTIVE_SOURCE_STATES
                    and (
                        collection.source_document_id is None
                        or collection.source_document_id == document.id
                    )
                ):
                    active_document_ids.add(document.id)
            enrichment_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
            )
            publication_artifact = await uow.production_artifacts.get_current(
                run.id, ProductionArtifactStage.PUBLICATION.value
            )
            media_records: tuple[SourceMediaRecord, ...] = ()
            if self._media_policy_sha256 is not None:
                media_records = tuple(
                    item
                    for item in await uow.source_media_candidates.list_for_subject_policy(
                        subject_id, self._media_policy_sha256
                    )
                    if item.source_document_id in active_document_ids
                )
                if len(media_records) > _MAX_CATALOG_SOURCE_MEDIA_RECORDS:
                    raise EditorialFigureCatalogError("figure_catalog_unavailable")
            cache_key = _catalog_cache_key(
                subject_id=subject_id,
                run=run,
                production_input=production_input,
                extraction_artifact=extraction_artifact,
                enrichment_artifact=enrichment_artifact,
                publication_artifact=publication_artifact,
                source_documents=tuple(source_documents.values()),
                collections=tuple(collections.values()),
                active_document_ids=frozenset(active_document_ids),
                media_policy_sha256=self._media_policy_sha256,
                media_records=media_records,
            )
            cached = self._cache.get(cache_key) if use_cache else None
            if cached is not None:
                if (
                    cached.access_extraction.subject_id != subject_id
                    or cached.access_extraction.production_input_hash != production_input.input_hash
                ):
                    raise EditorialFigureCatalogError("figure_catalog_unavailable")
                try:
                    access_policy = await build_synthesis_access_policy(
                        production_input,
                        cached.access_extraction,
                        uow.source_documents,
                        uow.source_collections,
                        current_subject_tlp=subject.tlp,
                    )
                except (AttributeError, TypeError, ValueError):
                    raise EditorialFigureCatalogError("figure_catalog_unavailable") from None
                if tuple(TLP).index(access_policy.effective_tlp) > tuple(TLP).index(
                    cached.effective_tlp
                ):
                    raise EditorialFigureCatalogError("editorial_figure_not_found")
                return cached.items

            extraction_payload = await self._read_artifact_json(uow, extraction_artifact)
            try:
                extraction = production_extraction_from_json(extraction_payload)
            except (TypeError, ValueError):
                raise EditorialFigureCatalogError("figure_catalog_unavailable") from None
            if (
                extraction.subject_id != subject_id
                or extraction.production_input_hash != production_input.input_hash
            ):
                raise EditorialFigureCatalogError("figure_catalog_unavailable")
            try:
                access_policy = await build_synthesis_access_policy(
                    production_input,
                    extraction,
                    uow.source_documents,
                    uow.source_collections,
                    current_subject_tlp=subject.tlp,
                )
            except (AttributeError, TypeError, ValueError):
                raise EditorialFigureCatalogError("figure_catalog_unavailable") from None
            # A local read does not submit the archived figure to a model.
            active_sources = tuple(
                source
                for source in extraction.sources
                if source.source_document_id in active_document_ids
            )
            if not active_sources:
                items: tuple[EditorialFigureCatalogItem, ...] = ()
                if use_cache:
                    self._cache.put(
                        cache_key,
                        _CachedFigureCatalog(
                            items=items,
                            access_extraction=_minimal_access_extraction(extraction),
                            effective_tlp=access_policy.effective_tlp,
                        ),
                    )
                return items

            await _ensure_inventory_bounds(
                uow,
                source_documents=tuple(source_documents.values()),
                active_document_ids=frozenset(active_document_ids),
                active_sources=active_sources,
                media_records=media_records,
            )

            filtered_documents = _FilteredSourceDocuments(
                uow.source_documents,
                tuple(
                    document
                    for document in source_documents.values()
                    if document.id in active_document_ids
                ),
                frozenset(active_document_ids),
            )
            source_media_repository = _FilteredSourceMedia(
                uow.source_media_candidates,
                frozenset(active_document_ids),
                media_records,
            )
            inventory = await load_archived_source_figure_inventory(
                subject_id=subject_id,
                extraction_sources=active_sources,
                source_document_repository=cast(SourceDocumentRepository, filtered_documents),
                blob_repository=uow.blobs,
                artifact_store=self._artifact_store,
                source_media_repository=(
                    cast(SourceMediaRepository, source_media_repository)
                    if self._media_policy_sha256 is not None
                    else None
                ),
                media_policy_sha256=self._media_policy_sha256,
            )
            try:
                figure_catalog = build_editorial_figure_catalog(extraction, inventory)
            except (TypeError, ValueError):
                raise EditorialFigureCatalogError("figure_catalog_unavailable") from None

            enrichment = await self._read_enrichment(uow, enrichment_artifact, subject_id, run.id)
            used_figure_ids: set[UUID] = set()
            used_content: set[tuple[UUID, str, str, int]] = set()
            not_selected_ids: set[UUID] = set()
            if enrichment is not None:
                for candidate in enrichment.source_figures:
                    resolved = candidate.resolved_figure
                    if candidate.inclusion_status.value == "included" and resolved is not None:
                        used_figure_ids.add(resolved.figure_id)
                        if (
                            resolved.sha256 is not None
                            and resolved.mime_type is not None
                            and resolved.byte_size is not None
                        ):
                            used_content.add(
                                (
                                    resolved.source_document_id,
                                    resolved.sha256,
                                    resolved.mime_type,
                                    resolved.byte_size,
                                )
                            )
                not_selected_ids.update(
                    item.figure_id
                    for item in enrichment.figure_decisions
                    if item.decision is EditorialFigureDecision.NOT_SELECTED_BY_MODEL
                )

            public_sources, published_content = await self._read_publication(
                uow, publication_artifact, subject_id
            )
            used_content.update(published_content)

        context = _CurrentFigureContext(
            subject_id=subject_id,
            run=run,
            extraction=extraction,
            inventory=inventory,
            figure_catalog=figure_catalog,
            source_documents=source_documents,
            public_sources=public_sources,
            used_figure_ids=frozenset(used_figure_ids),
            used_content=frozenset(used_content),
            not_selected_ids=frozenset(not_selected_ids),
        )
        items = self._items(context)
        if use_cache:
            self._cache.put(
                cache_key,
                _CachedFigureCatalog(
                    items=items,
                    access_extraction=_minimal_access_extraction(extraction),
                    effective_tlp=access_policy.effective_tlp,
                ),
            )
        return items

    async def _read_artifact_json(
        self, uow: UnitOfWork, artifact: ProductionArtifact
    ) -> dict[str, Any]:
        blob_id = artifact.canonical_blob_id
        if blob_id is None:
            raise EditorialFigureCatalogError("figure_catalog_unavailable")
        blob = await uow.blobs.get(blob_id)
        if blob is None or blob.descriptor.size > 4 * 1024 * 1024:
            raise EditorialFigureCatalogError("figure_catalog_unavailable")
        try:
            content = await self._artifact_store.read_bytes(blob_id, max_bytes=4 * 1024 * 1024)
            if (
                len(content) != blob.descriptor.size
                or hashlib.sha256(content).hexdigest() != blob.descriptor.sha256
            ):
                raise ValueError("artifact_content_mismatch")
            payload = json.loads(content)
        except (OSError, TypeError, ValueError, UnicodeDecodeError):
            raise EditorialFigureCatalogError("figure_catalog_unavailable") from None
        if not isinstance(payload, dict):
            raise EditorialFigureCatalogError("figure_catalog_unavailable")
        return payload

    async def _read_enrichment(
        self,
        uow: UnitOfWork,
        artifact: ProductionArtifact | None,
        subject_id: UUID,
        run_id: UUID,
    ) -> EditorialEnrichmentV1 | None:
        if artifact is None or artifact.status is not ProductionArtifactStatus.VERIFIED:
            return None
        payload = await self._read_artifact_json(uow, artifact)
        try:
            enrichment = editorial_enrichment_from_json(payload)
        except (TypeError, ValueError):
            raise EditorialFigureCatalogError("figure_catalog_unavailable") from None
        if enrichment.subject_id != subject_id:
            raise EditorialFigureCatalogError("figure_catalog_unavailable")
        return enrichment

    async def _read_publication(
        self,
        uow: UnitOfWork,
        artifact: ProductionArtifact | None,
        subject_id: UUID,
    ) -> tuple[Mapping[UUID, FigureCatalogSource], set[tuple[UUID, str, str, int]]]:
        if artifact is None or artifact.status is not ProductionArtifactStatus.VERIFIED:
            return {}, set()
        payload = await self._read_artifact_json(uow, artifact)
        try:
            publication = parse_publication_document(payload)
        except (TypeError, ValueError):
            raise EditorialFigureCatalogError("figure_catalog_unavailable") from None
        if getattr(publication, "subject_id", None) != subject_id:
            raise EditorialFigureCatalogError("figure_catalog_unavailable")
        source_values = getattr(publication, "sources", ())
        public_sources = {
            item.source_document_id: FigureCatalogSource(
                source_id=str(item.source_document_id), title=item.title, url=item.canonical_url
            )
            for item in source_values
        }
        figures = getattr(publication, "figures", ())
        used_content = {
            (item.source_document_id, item.sha256, item.mime_type, item.byte_size)
            for item in figures
        }
        return public_sources, used_content

    def _items(self, context: _CurrentFigureContext) -> tuple[EditorialFigureCatalogItem, ...]:
        items: list[EditorialFigureCatalogItem] = []
        for entry in context.figure_catalog:
            figure = entry.figure
            metadata = entry.metadata
            document = context.source_documents.get(figure.source_document_id)
            if document is None or figure.source_document_id not in {
                source.source_document_id for source in context.extraction.sources
            }:
                continue
            already_used = figure.figure_id in context.used_figure_ids or (
                figure.sha256 is not None
                and figure.mime_type is not None
                and figure.byte_size is not None
                and (
                    figure.source_document_id,
                    figure.sha256,
                    figure.mime_type,
                    figure.byte_size,
                )
                in context.used_content
            )
            not_selected = figure.figure_id in context.not_selected_ids
            eligibility = classify_source_figure(
                figure, already_used=already_used, not_selected=not_selected
            )
            public_source = context.public_sources.get(figure.source_document_id)
            original_caption = _bounded_text(
                metadata.caption_text or figure.locator.figure_label, _MAX_LABEL_CHARS
            )
            alt_text = _bounded_text(metadata.alt_text, _MAX_LABEL_CHARS)
            original_name = (
                original_caption
                or alt_text
                or _bounded_text(document.original_name, _MAX_LABEL_CHARS)
                or "Figure issue des sources"
            )
            items.append(
                EditorialFigureCatalogItem(
                    figure_id=opaque_figure_id(
                        context.subject_id, context.run.id, figure.figure_id
                    ),
                    original_name=original_name,
                    original_caption=original_caption,
                    alt_text=alt_text,
                    source=(
                        public_source
                        if public_source is not None
                        else FigureCatalogSource(str(figure.source_document_id), None, None)
                    ),
                    page=figure.locator.page,
                    section=_bounded_text(
                        metadata.nearby_heading_text or figure.locator.section, _MAX_LABEL_CHARS
                    ),
                    context_before=_bounded_text(metadata.context_before, _MAX_CONTEXT_CHARS),
                    context_after=_bounded_text(metadata.context_after, _MAX_CONTEXT_CHARS),
                    width=metadata.width,
                    height=metadata.height,
                    mime_type=figure.mime_type,
                    status=eligibility.status,
                    selectable=eligibility.selectable,
                    exclusion_reason=eligibility.exclusion_reason,
                    already_used=already_used,
                    _source_figure_id=figure.figure_id,
                    _source_document_id=figure.source_document_id,
                    _blob_id=figure.blob_id,
                    _sha256=figure.sha256,
                    _byte_size=figure.byte_size,
                    _run_id=context.run.id,
                    _inventory_max_figures=context.inventory.max_figures,
                    _resolved_figure=figure,
                )
            )
        return tuple(items)

    @staticmethod
    def _searchable_text(item: EditorialFigureCatalogItem) -> str:
        return " ".join(
            value
            for value in (
                item.original_name,
                item.original_caption or "",
                item.alt_text or "",
                item.source.title or "",
                item.source.source_id,
                item.section or "",
                item.context_before or "",
                item.context_after or "",
            )
            if value
        )


@dataclass(frozen=True, slots=True)
class _FilteredSourceDocuments:
    delegate: SourceDocumentRepository
    documents: tuple[Any, ...]
    active_document_ids: frozenset[UUID]

    async def add(self, document: Any) -> None:
        await self.delegate.add(document)

    async def get(self, document_id: UUID) -> Any | None:
        document = await self.delegate.get(document_id)
        return (
            document if document is not None and document.id in self.active_document_ids else None
        )

    async def save(self, document: Any) -> None:
        await self.delegate.save(document)

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Any]:
        return tuple(item for item in self.documents if item.subject_id == subject_id)


@dataclass(frozen=True, slots=True)
class _FilteredSourceMedia:
    delegate: SourceMediaRepository
    active_source_ids: frozenset[UUID]
    records: tuple[SourceMediaRecord, ...] | None = None

    async def get(self, candidate_id: UUID) -> SourceMediaRecord | None:
        record = await self.delegate.get(candidate_id)
        return (
            record
            if record is not None and record.source_document_id in self.active_source_ids
            else None
        )

    async def list_for_subject_policy(
        self, subject_id: UUID, policy_sha256: str
    ) -> Sequence[SourceMediaRecord]:
        if self.records is not None:
            return tuple(
                item
                for item in self.records
                if item.subject_id == subject_id
                and item.policy_sha256 == policy_sha256
                and item.source_document_id in self.active_source_ids
            )
        records = await self.delegate.list_for_subject_policy(subject_id, policy_sha256)
        return tuple(item for item in records if item.source_document_id in self.active_source_ids)

    async def add_if_absent(self, record: SourceMediaRecord) -> SourceMediaRecord:
        return await self.delegate.add_if_absent(record)


def _catalog_cache_key(
    *,
    subject_id: UUID,
    run: ProductionRun,
    production_input: Any,
    extraction_artifact: ProductionArtifact,
    enrichment_artifact: ProductionArtifact | None,
    publication_artifact: ProductionArtifact | None,
    source_documents: tuple[Any, ...],
    collections: tuple[Any, ...],
    active_document_ids: frozenset[UUID],
    media_policy_sha256: str | None,
    media_records: tuple[SourceMediaRecord, ...],
) -> tuple[UUID, UUID, str]:
    def artifact_pin(artifact: ProductionArtifact | None) -> dict[str, str | int] | None:
        if artifact is None:
            return None
        return {
            "id": str(artifact.id),
            "version": artifact.version,
            "input_hash": artifact.input_hash,
            "canonical_blob_id": str(artifact.canonical_blob_id),
            "status": artifact.status.value,
        }

    payload = {
        "subject_id": str(subject_id),
        "run_id": str(run.id),
        "pipeline_generation": run.pipeline_generation,
        "production_input": {
            "id": str(production_input.id),
            "input_hash": production_input.input_hash,
            "subject_version": production_input.subject_version,
        },
        "extraction": artifact_pin(extraction_artifact),
        "enrichment": artifact_pin(enrichment_artifact),
        "publication": artifact_pin(publication_artifact),
        "active_document_ids": sorted(map(str, active_document_ids)),
        "documents": [
            {
                "id": str(document.id),
                "blob_id": str(document.decoded_blob_id or document.blob_id),
                "mime_type": document.detected_mime_type,
                "original_name": document.original_name,
                "title": document.title,
                "publisher": document.publisher,
                "final_url": document.final_url,
                "collection_id": (
                    str(document.source_collection_id)
                    if document.source_collection_id is not None
                    else None
                ),
            }
            for document in sorted(source_documents, key=lambda item: item.id.hex)
        ],
        "collections": [
            {
                "id": str(collection.id),
                "state": collection.state.value,
                "source_document_id": (
                    str(collection.source_document_id)
                    if collection.source_document_id is not None
                    else None
                ),
            }
            for collection in sorted(collections, key=lambda item: item.id.hex)
        ],
        "media_policy_sha256": media_policy_sha256,
        "media_records": [
            {
                "id": str(record.id),
                "source_document_id": str(record.source_document_id),
                "blob_id": str(record.blob_id) if record.blob_id is not None else None,
                "sha256": record.sha256,
                "mime_type": record.mime_type,
                "policy_version": record.policy_version,
                "policy_sha256": record.policy_sha256,
                "width": record.width,
                "height": record.height,
                "byte_size": record.byte_size,
                "page": record.page,
                "page_bbox": record.page_bbox,
                "anchor": record.anchor,
                "dom_locator": record.dom_locator,
                "original_url": record.original_url,
                "requested_url": record.requested_url,
                "final_url": record.final_url,
                "alt_text": record.alt_text,
                "caption_text": record.caption_text,
                "nearby_heading_text": record.nearby_heading_text,
                "collection_diagnostics": record.collection_diagnostics,
                "perceptual_hash": record.perceptual_hash,
                "status": record.status.value,
                "reason_code": record.reason_code.value,
            }
            for record in sorted(media_records, key=lambda item: item.id.hex)
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return subject_id, run.id, digest


def _minimal_access_extraction(extraction: ProductionExtractionV1) -> ProductionExtractionV1:
    sources = tuple(
        replace(
            source,
            facts=(),
            events=(),
            indicators=(),
            rules=(),
            uncertainties=(),
            scope=None,
        )
        for source in extraction.sources
    )
    return ProductionExtractionV1(
        schema_version=extraction.schema_version,
        subject_id=extraction.subject_id,
        production_input_hash=extraction.production_input_hash,
        references_corpus_hash=extraction.references_corpus_hash,
        profile_policy_version=extraction.profile_policy_version,
        sources=sources,
        omitted_sources=(),
        warnings=(),
    )


async def _ensure_inventory_bounds(
    uow: UnitOfWork,
    *,
    source_documents: tuple[Any, ...],
    active_document_ids: frozenset[UUID],
    active_sources: tuple[Any, ...],
    media_records: tuple[SourceMediaRecord, ...],
) -> None:
    if (
        len(source_documents) > _MAX_CATALOG_SOURCE_DOCUMENTS
        or len(active_sources) > _MAX_CATALOG_SOURCE_DOCUMENTS
        or len(media_records) > _MAX_CATALOG_SOURCE_MEDIA_RECORDS
    ):
        raise EditorialFigureCatalogError("figure_catalog_unavailable")

    extraction_document_ids = {source.source_document_id for source in active_sources}
    total_bytes = sum(record.byte_size or 0 for record in media_records)
    active_documents = (
        document for document in source_documents if document.id in active_document_ids
    )
    for document in active_documents:
        blob_id = document.decoded_blob_id or document.blob_id
        blob = await uow.blobs.get(blob_id)
        if blob is None:
            continue
        mime_type = (document.detected_mime_type or blob.descriptor.mime_type).partition(";")[0]
        if mime_type.startswith("image/") and blob.descriptor.size <= MAX_SOURCE_FIGURE_BYTES:
            total_bytes += blob.descriptor.size
        elif (
            document.id in extraction_document_ids
            and mime_type in {"text/html", "application/xhtml+xml", "application/pdf"}
            and blob.descriptor.size <= MAX_SOURCE_DOCUMENT_BYTES
        ):
            total_bytes += blob.descriptor.size
        if total_bytes > _MAX_CATALOG_ARCHIVE_BYTES:
            raise EditorialFigureCatalogError("figure_catalog_unavailable")
    if total_bytes > _MAX_CATALOG_ARCHIVE_BYTES:
        raise EditorialFigureCatalogError("figure_catalog_unavailable")


def _bounded_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    if not compact:
        return None
    return compact[:limit]

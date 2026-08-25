"""Deterministic processing of archived source evidence."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Final
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.extraction import (
    PARSER_NAME,
    PARSER_VERSION,
    DocumentParsingError,
    PdfParsingPolicy,
    extract_indicators,
    parse_document,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.collection import (
    CollectionState,
    DerivedArtifact,
    DetectedMimeType,
    SourceCollection,
)

DERIVED_TEXT_BUCKET: Final = "derived-evidence-text"
DERIVED_TEXT_MIME_TYPE: Final = "text/plain; charset=utf-8"
MAX_DECODED_DOCUMENT_BYTES: Final = PdfParsingPolicy().max_document_bytes


@dataclass(frozen=True, slots=True)
class SourceEvidenceProcessingOutcome:
    collection_id: UUID
    status: str
    indicator_occurrences: int = 0
    error_code: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "collection_id": str(self.collection_id),
            "status": self.status,
            "indicator_occurrences": self.indicator_occurrences,
            "error_code": self.error_code,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class SourceEvidenceProcessingResult:
    sources_seen: int
    sources_processed: int
    sources_cached: int
    sources_failed: int
    indicator_occurrences: int
    outcomes: tuple[SourceEvidenceProcessingOutcome, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "sources_seen": self.sources_seen,
            "sources_processed": self.sources_processed,
            "sources_cached": self.sources_cached,
            "sources_failed": self.sources_failed,
            "indicator_occurrences": self.indicator_occurrences,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
        }


class _ProcessingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SourceEvidenceProcessingService:
    """Turn archived source blobs into deterministic evidence records.

    Blob I/O and parsing happen before acquiring the collection row lock. The
    final lock serializes publication of the artifact, indicators, and state.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        blob_catalog: BlobCatalogService,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_catalog = blob_catalog

    async def process_subject(self, subject_id: UUID) -> SourceEvidenceProcessingResult:
        async with self._uow_factory() as uow:
            collections = await uow.source_collections.list_for_subject(subject_id)

        candidates = tuple(
            collection
            for collection in collections
            if collection.state
            in {
                CollectionState.ARCHIVED,
                CollectionState.EXTRACTED,
                CollectionState.COMPLETED,
            }
        )
        outcomes_list: list[SourceEvidenceProcessingOutcome] = []
        for collection in candidates:
            outcomes_list.append(await self._process_collection(collection.id))
        outcomes = tuple(outcomes_list)
        return SourceEvidenceProcessingResult(
            sources_seen=len(candidates),
            sources_processed=sum(outcome.status == "processed" for outcome in outcomes),
            sources_cached=sum(outcome.status == "cached" for outcome in outcomes),
            sources_failed=sum(outcome.status == "failed" for outcome in outcomes),
            indicator_occurrences=sum(
                outcome.indicator_occurrences
                for outcome in outcomes
                if outcome.status == "processed"
            ),
            outcomes=outcomes,
        )

    async def _process_collection(self, collection_id: UUID) -> SourceEvidenceProcessingOutcome:
        try:
            return await self._process_collection_inner(collection_id)
        except _ProcessingError as error:
            return SourceEvidenceProcessingOutcome(
                collection_id=collection_id,
                status="failed",
                error_code=error.code,
                error=str(error),
            )
        except DocumentParsingError as error:
            return SourceEvidenceProcessingOutcome(
                collection_id=collection_id,
                status="failed",
                error_code="document_parsing_error",
                error=str(error),
            )
        except Exception as error:  # A bad source must not block its peers.
            return SourceEvidenceProcessingOutcome(
                collection_id=collection_id,
                status="failed",
                error_code="processing_error",
                error=str(error),
            )

    async def _process_collection_inner(
        self, collection_id: UUID
    ) -> SourceEvidenceProcessingOutcome:
        async with self._uow_factory() as uow:
            collection = await uow.source_collections.get(collection_id)
            if collection is None:
                raise _ProcessingError("collection_not_found", "Source collection no longer exists")
            cached = self._cached_outcome(collection)
            if cached is not None:
                return cached
            if collection.state is not CollectionState.ARCHIVED:
                raise _ProcessingError(
                    "collection_not_archived",
                    f"Source collection is {collection.state.value}, not ARCHIVED",
                )
            if collection.source_document_id is None:
                raise _ProcessingError(
                    "source_document_missing", "Archived collection has no source document"
                )
            source_document = await uow.source_documents.get(collection.source_document_id)
            if source_document is None:
                raise _ProcessingError(
                    "source_document_missing", "Source document no longer exists"
                )
            decoded_blob_id = collection.decoded_blob_id or source_document.decoded_blob_id
            if decoded_blob_id is None:
                raise _ProcessingError(
                    "decoded_blob_missing", "Source document has no decoded blob"
                )
            blob = await uow.blobs.get(decoded_blob_id)
            if blob is None:
                raise _ProcessingError("decoded_blob_missing", "Decoded blob no longer exists")
            mime_type = source_document.detected_mime_type or blob.descriptor.mime_type

        try:
            detected_mime_type = DetectedMimeType(mime_type)
        except ValueError as error:
            raise _ProcessingError(
                "unsupported_mime_type", f"Unsupported MIME type: {mime_type}"
            ) from error

        content = await self._blob_catalog.read(
            decoded_blob_id,
            max_bytes=MAX_DECODED_DOCUMENT_BYTES,
        )
        parsed = parse_document(content, detected_mime_type)
        text_blob = await self._blob_catalog.ingest(
            BytesIO(parsed.text.encode("utf-8")),
            logical_bucket=DERIVED_TEXT_BUCKET,
            mime_type=DERIVED_TEXT_MIME_TYPE,
        )
        artifact = DerivedArtifact(
            source_document_id=source_document.id,
            text_blob_id=text_blob.id,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            text_length=len(parsed.text),
            publication_metadata=dict(parsed.metadata),
        )
        indicators = extract_indicators(
            parsed.text,
            subject_id=collection.subject_id,
            edition_id=collection.edition_id,
            group_id=collection.group_id,
            source_document_id=source_document.id,
            artifact_id=artifact.id,
        )

        async with self._uow_factory() as uow:
            locked_collection = await uow.source_collections.get_for_update(collection_id)
            if locked_collection is None:
                raise _ProcessingError("collection_not_found", "Source collection no longer exists")
            cached = self._cached_outcome(locked_collection)
            if cached is not None:
                return cached
            if locked_collection.state is not CollectionState.ARCHIVED:
                raise _ProcessingError(
                    "collection_state_changed",
                    f"Source collection changed to {locked_collection.state.value}",
                )
            if locked_collection.source_document_id != source_document.id:
                raise _ProcessingError(
                    "source_document_changed", "Source document changed during processing"
                )

            await uow.derived_artifacts.append(artifact)
            await uow.indicators.append_many(indicators)
            locked_collection.extracted(artifact.id)
            await uow.source_collections.save(locked_collection)
            await uow.commit()

        return SourceEvidenceProcessingOutcome(
            collection_id=collection_id,
            status="processed",
            indicator_occurrences=len(indicators),
        )

    @staticmethod
    def _cached_outcome(collection: SourceCollection) -> SourceEvidenceProcessingOutcome | None:
        if (
            collection.state in {CollectionState.EXTRACTED, CollectionState.COMPLETED}
            and collection.derived_artifact_id is not None
        ):
            return SourceEvidenceProcessingOutcome(collection.id, "cached")
        return None

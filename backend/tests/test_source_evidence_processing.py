from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.source_evidence_processing import SourceEvidenceProcessingService
from cti_app.domain.collection import CollectionState
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.test_collection import Transport, response, selected_subject, service


async def _archived_sources(
    factory: InMemoryCollectionUnitOfWorkFactory,
    root: Path,
    *bodies: bytes,
) -> tuple[SourceEvidenceProcessingService, list[object]]:
    subject = selected_subject(
        factory,
        tuple(f"https://source-{index}.example/report" for index in range(len(bodies))),
    )
    collector = service(factory, Transport([response(body) for body in bodies]), root / "blobs")
    collections = await collector.initialize(subject.id)
    for collection in collections:
        await collector.archive_one(collection.id, uuid4())
    processor = SourceEvidenceProcessingService(
        factory,
        BlobCatalogService(FilesystemBlobStore(root / "blobs"), factory),  # type: ignore[arg-type]
    )
    return processor, collections


async def test_archived_source_becomes_extracted_with_artifact_and_indicators(
    tmp_path: Path,
) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    processor, collections = await _archived_sources(
        factory,
        tmp_path,
        b"<html><body>evil.example 2001:db8::1 sha512 " + b"a" * 128 + b"</body></html>",
    )
    collection = collections[0]

    result = await processor.process_subject(collection.subject_id)

    assert result.sources_seen == 1
    assert result.sources_processed == 1
    assert result.sources_failed == 0
    assert result.indicator_occurrences >= 3
    persisted = factory.collections[collection.id]
    assert persisted.state is CollectionState.EXTRACTED
    assert persisted.derived_artifact_id is not None
    assert len(factory.artifacts) == 1
    assert len(factory.indicators) == result.indicator_occurrences

    cached = await processor.process_subject(collection.subject_id)

    assert cached.sources_cached == 1
    assert cached.sources_processed == 0
    assert len(factory.artifacts) == 1
    assert len(factory.indicators) == result.indicator_occurrences


async def test_one_unparseable_archived_source_does_not_rollback_others(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    processor, collections = await _archived_sources(
        factory,
        tmp_path,
        b"<html><body>evil.example 198.51.100.10</body></html>",
        b"<html><body>not supported as an input type</body></html>",
    )
    invalid = collections[1]
    invalid_document_id = factory.collections[invalid.id].source_document_id
    assert invalid_document_id is not None
    factory.documents[invalid_document_id].detected_mime_type = "application/octet-stream"

    result = await processor.process_subject(collections[0].subject_id)

    assert result.sources_seen == 2
    assert result.sources_processed == 1
    assert result.sources_failed == 1
    assert result.outcomes[1].error_code == "unsupported_mime_type"
    assert factory.collections[collections[0].id].state is CollectionState.EXTRACTED
    assert factory.collections[invalid.id].state is CollectionState.ARCHIVED
    assert len(factory.artifacts) == 1

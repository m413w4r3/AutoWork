"""Deterministic source-to-document provenance helpers."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from cti_app.application.production_parsers import ReferenceReport
from cti_app.domain.collection import SourceCollection
from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.entities import SourceDocument


def source_ids_by_document(
    collections: Sequence[SourceCollection],
    documents: Sequence[SourceDocument],
    report: ReferenceReport,
) -> dict[UUID, tuple[str, ...]]:
    report_ids: dict[str, str] = {}
    for source in report.sources:
        try:
            report_ids[canonicalize_http_url(source.canonical_url)] = source.local_id
        except ValueError:
            continue
    docs = {document.id: document for document in documents}
    by_id = {collection.id: collection for collection in collections}
    result: dict[UUID, tuple[str, ...]] = {}
    for collection in sorted(collections, key=lambda item: str(item.id)):
        root = collection
        seen: set[UUID] = set()
        while root.parent_source_collection_id is not None and root.id not in seen:
            seen.add(root.id)
            root = by_id.get(root.parent_source_collection_id, root)
            if root.id in seen:
                break
        document = (
            docs.get(collection.source_document_id) if collection.source_document_id else None
        )
        values = (
            (root.canonical_url,)
            if collection.parent_source_collection_id
            else (document.final_url if document else None, root.canonical_url)
        )
        source_id = next(
            (
                sid
                for value in values
                if value and _is_url(value)
                for sid in (report_ids.get(canonicalize_http_url(value)),)
                if sid is not None
            ),
            None,
        )
        if source_id and collection.source_document_id:
            result[collection.source_document_id] = tuple(
                sorted(set((*result.get(collection.source_document_id, ()), source_id)))
            )
    return result


def _is_url(value: str) -> bool:
    try:
        canonicalize_http_url(value)
    except ValueError:
        return False
    return True

"""Build the explicit, archived corpus sent to Q2.

This module is deliberately free of model and conversation dependencies.  It
turns persisted archive snapshots into bounded, hashable chunks.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from cti_app.application.extraction import CHUNKING_VERSION, ChunkingPolicy, segment_text
from cti_app.application.production_parsers import ReferenceReport
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.entities import SourceDocument

PACK_VERSION = "production-evidence-pack-v1"
PARSER_VERSIONS = {"pack": PACK_VERSION, "chunking": CHUNKING_VERSION}
DEFAULT_ABSOLUTE_MAX_DOCUMENT_CHARS = 2_000_000


@dataclass(frozen=True, slots=True)
class ArchivedCorpusDocument:
    """One archived document plus its already-derived text.

    ``collection`` must be the persisted collection owning ``document``.
    Keeping this boundary explicit prevents callers from passing model text or
    text reconstructed from an IOC value.
    """

    collection: SourceCollection
    document: SourceDocument
    text: str


@dataclass(frozen=True, slots=True)
class EvidenceChunk:
    source_document_id: UUID
    parent_source_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    title: str | None
    origin_kind: SourceOriginKind
    chunk_id: str
    text: str
    sha256: str
    internal_metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProductionEvidencePack:
    status: str
    pack_hash: str
    chunks: tuple[EvidenceChunk, ...]
    parser_versions: Mapping[str, str]
    error_code: str | None = None
    error_message: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.status == "needs_review"

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        return tuple(chunk.chunk_id for chunk in self.chunks)


def build_production_evidence_pack(
    reference_report: ReferenceReport,
    archived_publications: Sequence[ArchivedCorpusDocument],
    archived_referenced_evidence: Sequence[ArchivedCorpusDocument] = (),
    *,
    chunking_policy: ChunkingPolicy | None = None,
    absolute_max_document_chars: int = DEFAULT_ABSOLUTE_MAX_DOCUMENT_CHARS,
) -> ProductionEvidencePack:
    """Build Q2 corpus from archived publications and first-level evidence.

    Documents not represented by Q1 sources, or child evidence whose parent is
    not an archived Q1 publication, are excluded.  Invalid oversized input
    returns an explicit review state and never gets silently truncated.
    """
    if absolute_max_document_chars <= 0:
        raise ValueError("absolute_max_document_chars must be positive")

    source_by_url = {source.canonical_url: source for source in reference_report.sources}
    publications: dict[UUID, ArchivedCorpusDocument] = {}
    for item in archived_publications:
        collection = item.collection
        if (
            not _archived(collection)
            or collection.origin_kind is SourceOriginKind.REFERENCED_EVIDENCE
        ):
            continue
        source = source_by_url.get(collection.canonical_url)
        if source is not None and collection.source_document_id == item.document.id:
            publications[item.document.id] = item

    publication_collection_ids = {item.collection.id for item in publications.values()}
    selected = list(publications.values())
    for item in archived_referenced_evidence:
        collection = item.collection
        if (
            _archived(collection)
            and collection.origin_kind is SourceOriginKind.REFERENCED_EVIDENCE
            and collection.parent_source_collection_id in publication_collection_ids
            and collection.source_document_id == item.document.id
        ):
            selected.append(item)

    chunks: list[EvidenceChunk] = []
    for item in sorted(selected, key=lambda value: str(value.document.id)):
        if len(item.text) > absolute_max_document_chars:
            return _review_pack(
                "document_text_too_large",
                f"Document {item.document.id} exceeds absolute character limit",
            )
        if not item.text:
            return _review_pack(
                "derived_text_empty", f"Document {item.document.id} has no derived text"
            )

        source_ids = _source_ids(reference_report, item, publications)
        parent_source_ids = source_ids if item.collection.parent_source_collection_id else ()
        for index, chunk in enumerate(segment_text(item.text, chunking_policy)):
            chunk_id = f"{item.document.id}:{index:06d}:{chunk.sha256[:16]}"
            chunks.append(
                EvidenceChunk(
                    source_document_id=item.document.id,
                    parent_source_ids=parent_source_ids,
                    source_ids=source_ids,
                    title=item.document.title or item.collection.title,
                    origin_kind=item.collection.origin_kind,
                    chunk_id=chunk_id,
                    text=chunk.text,
                    sha256=chunk.sha256,
                    internal_metadata={"canonical_source_url": _canonical_url(item, publications)},
                )
            )

    payload = [
        {
            "source_document_id": str(chunk.source_document_id),
            "parent_source_ids": chunk.parent_source_ids,
            "source_ids": chunk.source_ids,
            "title": chunk.title,
            "origin_kind": chunk.origin_kind.value,
            "chunk_id": chunk.chunk_id,
            "text": chunk.text,
            "sha256": chunk.sha256,
            "internal_metadata": dict(chunk.internal_metadata),
        }
        for chunk in chunks
    ]
    pack_hash = hashlib.sha256(
        json.dumps(
            {"version": PACK_VERSION, "parser_versions": PARSER_VERSIONS, "chunks": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ProductionEvidencePack("ready", pack_hash, tuple(chunks), PARSER_VERSIONS)


def _archived(collection: SourceCollection) -> bool:
    return collection.state in {
        CollectionState.ARCHIVED,
        CollectionState.EXTRACTED,
        CollectionState.COMPLETED,
    }


def _source_ids(
    report: ReferenceReport,
    item: ArchivedCorpusDocument,
    publications: Mapping[UUID, ArchivedCorpusDocument],
) -> tuple[str, ...]:
    if item.collection.parent_source_collection_id:
        parent = next(
            (
                value
                for value in publications.values()
                if value.collection.id == item.collection.parent_source_collection_id
            ),
            None,
        )
        if parent is None:
            return ()
        url = parent.collection.canonical_url
    else:
        url = item.collection.canonical_url
    return tuple(source.local_id for source in report.sources if source.canonical_url == url)


def _canonical_url(
    item: ArchivedCorpusDocument,
    publications: Mapping[UUID, ArchivedCorpusDocument],
) -> str:
    if item.collection.parent_source_collection_id:
        parent = next(
            (
                value
                for value in publications.values()
                if value.collection.id == item.collection.parent_source_collection_id
            ),
            None,
        )
        return parent.collection.canonical_url if parent else item.collection.canonical_url
    return item.collection.canonical_url


def _review_pack(code: str, message: str) -> ProductionEvidencePack:
    return ProductionEvidencePack(
        status="needs_review",
        pack_hash="",
        chunks=(),
        parser_versions=PARSER_VERSIONS,
        error_code=code,
        error_message=message,
    )

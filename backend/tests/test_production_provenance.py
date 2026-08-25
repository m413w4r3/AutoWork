from datetime import UTC, datetime
from uuid import UUID, uuid4

from cti_app.application.production_parsers import ParsedSource, ReferenceReport
from cti_app.application.production_provenance import source_ids_by_document
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument


def _collection(
    *,
    url: str,
    subject_id: UUID,
    document_id: UUID,
    parent: UUID | None = None,
) -> SourceCollection:
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=uuid4(),
        group_id=uuid4(),
        requested_url=url,
        canonical_url=url,
        proposed_role=SourceRole.PRIMARY,
        origin_kind=(
            SourceOriginKind.REFERENCED_EVIDENCE if parent else SourceOriginKind.DISCOVERY
        ),
        parent_source_collection_id=parent,
        source_document_id=document_id,
        state=CollectionState.EXTRACTED,
    )
    collection.id = uuid4()
    return collection


def _document(
    *, subject_id: UUID, collection_id: UUID, final_url: str | None = None
) -> SourceDocument:
    return SourceDocument(
        subject_id=subject_id,
        blob_id=uuid4(),
        original_name="source.html",
        origin=final_url or "https://example.test/",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.CLEAR,
        do_not_submit=False,
        external_llm_allowed=True,
        source_collection_id=collection_id,
        title="Title",
        final_url=final_url,
        id=uuid4(),
    )


def _report(url: str) -> ReferenceReport:
    return ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title="Title",
                url=url,
                canonical_url=url,
                publisher="Publisher",
                published_at=None,
                role=SourceRole.PRIMARY,
            ),
        ),
        events=(),
    )


def test_final_url_matches_directly() -> None:
    subject_id, document_id = uuid4(), uuid4()
    collection = _collection(
        url="https://vendor.example/report", subject_id=subject_id, document_id=document_id
    )
    document = _document(
        subject_id=subject_id,
        collection_id=collection.id,
        final_url="https://vendor.example/report",
    )
    report = _report("https://vendor.example/report")

    result = source_ids_by_document([collection], [document], report)

    assert result[document_id] == ("S1",)


def test_final_url_redirected_but_root_canonical_url_matches() -> None:
    """final_url differs after a redirect; the collection's canonical_url still resolves the S#."""
    subject_id, document_id = uuid4(), uuid4()
    collection = _collection(
        url="https://vendor.example/report", subject_id=subject_id, document_id=document_id
    )
    document = _document(
        subject_id=subject_id,
        collection_id=collection.id,
        final_url="https://www.vendor.example/research/report",
    )
    report = _report("https://vendor.example/report")

    result = source_ids_by_document([collection], [document], report)

    assert result[document_id] == ("S1",)


def test_child_evidence_inherits_parent_source_id() -> None:
    subject_id = uuid4()
    parent_document_id, child_document_id = uuid4(), uuid4()
    parent = _collection(
        url="https://vendor.example/report", subject_id=subject_id, document_id=parent_document_id
    )
    child = _collection(
        url="https://vendor.example/report/resource",
        subject_id=subject_id,
        document_id=child_document_id,
        parent=parent.id,
    )
    parent_document = _document(subject_id=subject_id, collection_id=parent.id)
    child_document = _document(subject_id=subject_id, collection_id=child.id)
    report = _report("https://vendor.example/report")

    result = source_ids_by_document(
        [parent, child], [parent_document, child_document], report
    )

    assert result[parent_document_id] == ("S1",)
    assert result[child_document_id] == ("S1",)


def test_redirect_and_canonicalization_are_equivalent() -> None:
    """A tracking-parameter/case redirect must not break provenance resolution."""
    subject_id, document_id = uuid4(), uuid4()
    collection = _collection(
        url="https://vendor.example/report", subject_id=subject_id, document_id=document_id
    )
    document = _document(
        subject_id=subject_id,
        collection_id=collection.id,
        final_url="https://VENDOR.example/report?utm_source=feed",
    )
    report = _report("https://vendor.example/report")

    result = source_ids_by_document([collection], [document], report)

    assert result[document_id] == ("S1",)


def test_parent_cycle_does_not_loop() -> None:
    """A malformed parent cycle must resolve deterministically, never hang."""
    subject_id = uuid4()
    doc_a, doc_b = uuid4(), uuid4()
    collection_a = _collection(
        url="https://vendor.example/a", subject_id=subject_id, document_id=doc_a
    )
    collection_b = _collection(
        url="https://vendor.example/b",
        subject_id=subject_id,
        document_id=doc_b,
        parent=collection_a.id,
    )
    # Introduce a cycle: a's parent points back at b.
    collection_a.parent_source_collection_id = collection_b.id
    document_a = _document(subject_id=subject_id, collection_id=collection_a.id)
    document_b = _document(subject_id=subject_id, collection_id=collection_b.id)
    report = _report("https://vendor.example/a")

    # Must terminate and simply fail to resolve rather than looping forever.
    result = source_ids_by_document(
        [collection_a, collection_b], [document_a, document_b], report
    )

    assert isinstance(result, dict)

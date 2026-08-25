from datetime import UTC, datetime
from uuid import uuid4

from cti_app.application.production_evidence_pack import (
    ArchivedCorpusDocument,
    build_production_evidence_pack,
)
from cti_app.application.production_parsers import ParsedSource, ReferenceReport
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument


def _item(
    *,
    url: str,
    origin: SourceOriginKind = SourceOriginKind.DISCOVERY,
    parent=None,
    text="x",
    final_url: str | None = None,
):
    subject_id, edition_id, group_id = uuid4(), uuid4(), uuid4()
    document_id, collection_id = uuid4(), uuid4()
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        requested_url=url,
        canonical_url=url,
        proposed_role=SourceRole.PRIMARY,
        origin_kind=origin,
        parent_source_collection_id=parent,
        source_document_id=document_id,
        state=CollectionState.EXTRACTED,
    )
    collection.id = collection_id
    document = SourceDocument(
        subject_id=subject_id,
        blob_id=uuid4(),
        original_name="source.html",
        origin=url,
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.CLEAR,
        do_not_submit=False,
        external_llm_allowed=True,
        source_collection_id=collection_id,
        title="Title",
        final_url=final_url,
        id=document_id,
    )
    return ArchivedCorpusDocument(collection, document, text)


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


def test_pack_hash_and_chunks_are_stable_and_overlap():
    item = _item(url="https://example.test/a", text="a" * 12_100)
    first = build_production_evidence_pack(_report(item.collection.canonical_url), [item])
    second = build_production_evidence_pack(_report(item.collection.canonical_url), [item])

    assert first.pack_hash == second.pack_hash
    assert first.chunks[0].text[-500:] == first.chunks[1].text[:500]
    assert first.chunk_ids == second.chunk_ids


def test_child_evidence_inherits_parent_s1_and_unarchived_is_absent():
    parent = _item(url="https://example.test/a", text="publication")
    child = _item(
        url="https://example.test/a/resource",
        origin=SourceOriginKind.REFERENCED_EVIDENCE,
        parent=parent.collection.id,
        text="resource",
    )
    absent = _item(url="https://example.test/not-in-q1", text="absent")
    pack = build_production_evidence_pack(
        _report(parent.collection.canonical_url), [parent, absent], [child]
    )

    assert len(pack.chunks) == 2
    resource = next(chunk for chunk in pack.chunks if chunk.source_document_id == child.document.id)
    assert resource.parent_source_ids == ("S1",)
    assert resource.source_ids == ("S1",)


def test_text_change_changes_hash_and_oversize_is_explicit():
    item = _item(url="https://example.test/a", text="before")
    changed = _item(url="https://example.test/a", text="after")
    report = _report(item.collection.canonical_url)
    first_hash = build_production_evidence_pack(report, [item]).pack_hash
    changed_hash = build_production_evidence_pack(report, [changed]).pack_hash
    assert first_hash != changed_hash

    review = build_production_evidence_pack(report, [item], absolute_max_document_chars=3)
    assert review.status == "needs_review"
    assert review.error_code == "document_text_too_large"


def test_pack_hash_and_metadata_include_derived_parser_identity():
    item = _item(url="https://example.test/a", text="archived")
    artifact_id = uuid4()
    first = ArchivedCorpusDocument(
        item.collection,
        item.document,
        item.text,
        derived_artifact_id=artifact_id,
        parser_version="parser-1",
        source_document_id=item.document.id,
    )
    second = ArchivedCorpusDocument(
        item.collection,
        item.document,
        item.text,
        derived_artifact_id=artifact_id,
        parser_version="parser-2",
        source_document_id=item.document.id,
    )
    report = _report(item.collection.canonical_url)
    first_pack = build_production_evidence_pack(report, [first])
    second_pack = build_production_evidence_pack(report, [second])

    assert first_pack.pack_hash != second_pack.pack_hash
    assert first_pack.parser_versions[f"artifact:{artifact_id}"] == "parser-1"
    assert second_pack.parser_versions[f"artifact:{artifact_id}"] == "parser-2"


def test_pack_uses_source_mapping_and_keeps_child_resource_url():
    parent = _item(
        url="https://example.test/report/",
        final_url="https://EXAMPLE.test/report/?utm_source=feed",
        text="publication",
    )
    child = _item(
        url="https://download.example.test/iocs.json/",
        final_url="https://DOWNLOAD.example.test/iocs.json/?utm_source=feed",
        origin=SourceOriginKind.REFERENCED_EVIDENCE,
        parent=parent.collection.id,
        text="resource",
    )

    pack = build_production_evidence_pack(
        _report("https://example.test/report"), [parent], [child]
    )
    resource = next(chunk for chunk in pack.chunks if chunk.source_document_id == child.document.id)

    assert resource.source_ids == ("S1",)
    assert resource.parent_source_ids == ("S1",)
    assert resource.internal_metadata["parent_canonical_url"] == "https://example.test/report"
    assert resource.internal_metadata["canonical_source_url"] == (
        "https://download.example.test/iocs.json"
    )

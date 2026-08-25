from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.extraction import PARSER_VERSION, ParsedLink
from cti_app.application.production_parsers import parse_reference_report
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.source_evidence_processing import (
    SourceEvidenceProcessingService,
    select_technical_links,
)
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.test_collection import Transport, response, selected_subject, service


def test_select_technical_links_resolves_only_safe_first_level_resources() -> None:
    selected = select_technical_links(
        "https://reports.example/advisory/index.html",
        (
            ParsedLink("iocs.json", "IOC download"),
            ParsedLink("https://cdn.example/report.pdf", "PDF"),
            ParsedLink("mailto:analyst@example", "contact"),
            ParsedLink("javascript:alert(1)", "download"),
            ParsedLink("data:text/plain,indicator", "download"),
            ParsedLink("#technical-annex", "annex"),
            ParsedLink("/advisory/index.html#again", "self"),
            ParsedLink("/login", "download"),
            ParsedLink("/assets/logo.png", "download"),
            ParsedLink("/privacy", "IOC list"),
            ParsedLink("/news", "read more"),
        ),
    )

    assert selected == (
        ("https://cdn.example/report.pdf", "PDF"),
        ("https://reports.example/advisory/iocs.json", "IOC download"),
    )


def test_select_technical_links_applies_a_deterministic_parent_cap() -> None:
    selected = select_technical_links(
        "https://reports.example/advisory",
        tuple(ParsedLink(f"/files/{number:02}.json", "IOC") for number in range(12)),
    )

    assert len(selected) == 8
    assert [url for url, _ in selected] == [
        f"https://reports.example/files/{number:02}.json" for number in range(8)
    ]


@pytest.mark.parametrize(
    "anchor_text",
    (
        "WHOIS Database Download",
        "DNS Database Download",
        "Download",
        "Download report",
        "Product database download",
    ),
)
def test_select_technical_links_rejects_download_without_technical_signal(
    anchor_text: str,
) -> None:
    assert select_technical_links(
        "https://reports.example/advisory",
        (ParsedLink("/download", anchor_text),),
    ) == ()


@pytest.mark.parametrize(
    ("href", "anchor_text"),
    (
        ("/download", "Download IOC list"),
        ("/download", "Indicators of Compromise"),
        ("/download", "IOC appendix"),
        ("/iocs.csv", "Download"),
        ("/indicators.json", "Download"),
        ("/download", "YARA rules"),
        ("/download", "Sigma rules"),
        ("/technical-appendix.pdf", "Download report"),
    ),
)
def test_select_technical_links_accepts_technical_signals_or_extensions(
    href: str,
    anchor_text: str,
) -> None:
    assert select_technical_links(
        "https://reports.example/advisory",
        (ParsedLink(href, anchor_text),),
    ) == ((f"https://reports.example{href}", anchor_text),)


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


async def test_stale_parser_artifact_is_reprocessed_and_becomes_current(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    processor, collections = await _archived_sources(
        factory, tmp_path, b"<html><body>evil.example 198.51.100.10</body></html>"
    )
    collection = collections[0]
    await processor.process_subject(collection.subject_id)
    original_artifact_id = factory.collections[collection.id].derived_artifact_id
    assert original_artifact_id is not None
    factory.artifacts[original_artifact_id] = replace(
        factory.artifacts[original_artifact_id], parser_version="obsolete-parser"
    )
    original_indicator_ids = set(factory.indicators)

    result = await processor.process_subject(collection.subject_id)

    current = factory.collections[collection.id].derived_artifact_id
    assert result.sources_processed == 1
    assert result.sources_cached == 0
    assert current is not None and current != original_artifact_id
    assert factory.artifacts[original_artifact_id].parser_version == "obsolete-parser"
    assert factory.artifacts[current].parser_version == PARSER_VERSION
    assert original_indicator_ids < set(factory.indicators)

    report_result = parse_reference_report(
        "## SOURCE S1\ntitle: Source\nurl: https://source-0.example/report\nrole: primary"
        "\n\n## EVENT R1\ndate: 2026-08-25\nsources: S1\ntext: Evidence processed.",
        date(2026, 8, 25),
    )
    assert report_result.value is not None
    workflow = object.__new__(ProductionWorkflowOrchestrator)
    workflow._source_evidence_processor = None
    async with factory() as uow:
        pack = await workflow._build_ioc_candidate_pack(
            uow, collection.subject_id, report_result.value
        )
    assert {
        evidence.derived_artifact_id
        for candidate in pack.candidates
        for evidence in candidate.evidence
    } == {current}


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


async def test_referenced_evidence_selection_never_traverses_a_child(tmp_path: Path) -> None:
    factory = InMemoryCollectionUnitOfWorkFactory()
    processor, collections = await _archived_sources(
        factory,
        tmp_path,
        b'<html><a href="/root-iocs.json">root IOC</a></html>',
    )
    parent = collections[0]
    child = SourceCollection(
        subject_id=parent.subject_id,
        edition_id=parent.edition_id,
        group_id=parent.group_id,
        requested_url="https://source-0.example/child.html",
        canonical_url="https://source-0.example/child.html",
        proposed_role=parent.proposed_role,
        origin_kind=SourceOriginKind.REFERENCED_EVIDENCE,
        parent_source_collection_id=parent.id,
        state=CollectionState.ARCHIVED,
        source_document_id=parent.source_document_id,
        decoded_blob_id=parent.decoded_blob_id,
    )
    factory.collections[child.id] = child

    selected = await processor.select_referenced_evidence(parent.subject_id)

    assert [(item.parent_source_collection_id, item.url) for item in selected] == [
        (parent.id, "https://source-0.example/root-iocs.json")
    ]

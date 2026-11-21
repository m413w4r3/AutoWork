from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_figure_catalog import (
    EditorialFigureCatalogError,
    EditorialFigureCatalogItem,
    FigureCatalogSource,
    archive_reference_for_catalog_item,
    assert_archive_reference_matches,
    validate_figure_insertion_limit,
)
from cti_app.domain.editorial_proposal import ManualSourceFigureSelection
from cti_app.domain.production_editorial_enrichment import (
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.domain.publication_kind import PublicationKind


def _catalog_item() -> EditorialFigureCatalogItem:
    content = b"synthetic archived image"
    content_sha256 = hashlib.sha256(content).hexdigest()
    source_document_id = uuid4()
    source = "https://sources.example.test/article"
    locator = SourceFigureLocatorV1(page=2, figure_label="Figure 2")
    resolved = ResolvedSourceFigureV1(
        figure_id=source_figure_id(
            source_document_id=source_document_id,
            sha256=content_sha256,
            source=source,
            locator=locator,
        ),
        blob_id=uuid4(),
        sha256=content_sha256,
        mime_type="image/png",
        byte_size=len(content),
        source_document_id=source_document_id,
        source=source,
        provenance="Archived source document, page 2",
        locator=locator,
        decision=SourceFigureDecision.ACCEPTED,
        decision_reason="archived_blob_verified",
    )
    return EditorialFigureCatalogItem(
        figure_id=str(uuid4()),
        original_name="Synthetic source figure",
        original_caption="Archived caption",
        alt_text=None,
        source=FigureCatalogSource(str(source_document_id), "Synthetic source", source),
        page=2,
        section="Analysis",
        context_before=None,
        context_after=None,
        width=320,
        height=180,
        mime_type="image/png",
        status="available",
        selectable=True,
        exclusion_reason=None,
        already_used=False,
        _source_figure_id=resolved.figure_id,
        _source_document_id=source_document_id,
        _blob_id=resolved.blob_id,
        _sha256=content_sha256,
        _byte_size=len(content),
        _run_id=uuid4(),
        _resolved_figure=resolved,
    )


def test_multi_figure_patch_rechecks_archive_sha_mime_size_dimensions() -> None:
    item = _catalog_item()
    reference = archive_reference_for_catalog_item(item)

    assert_archive_reference_matches(reference, item)
    assert not assert_archive_reference_matches(replace(reference, sha256="f" * 64), item)
    assert not assert_archive_reference_matches(replace(reference, mime_type="image/jpeg"), item)
    assert not assert_archive_reference_matches(
        replace(reference, byte_size=reference.byte_size + 1), item
    )
    assert not assert_archive_reference_matches(replace(reference, width=640), item)
    assert not assert_archive_reference_matches(replace(reference, height=360), item)


def test_brief_blocks_more_than_three_integrated_figures() -> None:
    with pytest.raises(EditorialFigureCatalogError, match="brief_figure_limit_exceeded"):
        validate_figure_insertion_limit(
            publication_kind=PublicationKind.BRIEF,
            integrated_count=2,
            requested_count=2,
            inventory_limit=12,
        )


def test_article_respects_the_archived_inventory_figure_limit() -> None:
    with pytest.raises(
        EditorialFigureCatalogError,
        match="source_figure_inventory_limit_exceeded",
    ):
        validate_figure_insertion_limit(
            publication_kind=PublicationKind.ARTICLE,
            integrated_count=1,
            requested_count=2,
            inventory_limit=2,
        )


def test_ineligible_or_pending_figure_refused() -> None:
    item = _catalog_item()
    pending = replace(item, status="unverified", selectable=False)

    with pytest.raises(EditorialFigureCatalogError, match="editorial_figure_not_eligible"):
        archive_reference_for_catalog_item(pending)


def test_manual_figure_selection_contains_only_catalog_id_caption_and_order() -> None:
    selection = ManualSourceFigureSelection(
        figure_id=UUID("10000000-0000-4000-8000-000000000001"),
        caption="Synthetic editorial caption",
        order=0,
    )

    assert selection.figure_id == UUID("10000000-0000-4000-8000-000000000001")
    assert selection.caption == "Synthetic editorial caption"
    assert selection.order == 0
    assert not hasattr(selection, "blob_id")

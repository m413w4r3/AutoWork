from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from cti_app.application.source_figure_inventory import (
    ArchivedSourceAsset,
    ArchivedSourceDocumentSnapshot,
    SourceFigureInventoryError,
    UnsupportedSourceFigureFormatError,
)
from cti_app.domain.production_source_figures import SourceFigureOriginKind
from cti_app.infrastructure.archived_html_source_figure_inventory import (
    ArchivedHtmlSourceFigureInventory,
)

SOURCE_ID = UUID("c586436c-025c-4ec9-9d43-21919d759361")


def _asset(path: str, content: bytes = b"image-bytes") -> ArchivedSourceAsset:
    return ArchivedSourceAsset(path=path, media_type="image/png", content=content)


def _source(
    html: str,
    *assets: ArchivedSourceAsset,
    media_type: str = "text/html",
) -> ArchivedSourceDocumentSnapshot:
    return ArchivedSourceDocumentSnapshot(
        source_document_id=SOURCE_ID,
        media_type=media_type,
        body_bytes=html.encode("utf-8"),
        local_assets=tuple(assets),
    )


@pytest.mark.asyncio
async def test_no_images_produces_empty_tuple() -> None:
    assert await ArchivedHtmlSourceFigureInventory().inventory(_source("<p>text</p>")) == ()


@pytest.mark.asyncio
async def test_local_images_follow_occurrence_order_and_keep_exact_bytes() -> None:
    first_bytes = b"\x89PNG\r\n\x00first"
    second_bytes = b"\x00second\xff"
    source = _source(
        '<img src="images/second.png"><img src="images/first.png">',
        _asset("images/first.png", first_bytes),
        _asset("images/second.png", second_bytes),
    )

    candidates = await ArchivedHtmlSourceFigureInventory().inventory(source)

    assert [candidate.key for candidate in candidates] == ["figure-001", "figure-002"]
    assert [candidate.provenance.source_locator for candidate in candidates] == [
        "images/second.png",
        "images/first.png",
    ]
    assert [candidate.media_bytes for candidate in candidates] == [second_bytes, first_bytes]
    assert [candidate.media_sha256 for candidate in candidates] == [
        hashlib.sha256(second_bytes).hexdigest(),
        hashlib.sha256(first_bytes).hexdigest(),
    ]
    assert all(candidate.source_document_id == SOURCE_ID for candidate in candidates)
    assert all(
        candidate.provenance.origin_kind is SourceFigureOriginKind.HTML_IMAGE
        for candidate in candidates
    )


@pytest.mark.asyncio
async def test_repeated_occurrences_of_same_asset_remain_distinct() -> None:
    source = _source(
        '<img src="same.png"><img src="same.png">',
        _asset("same.png"),
    )

    candidates = await ArchivedHtmlSourceFigureInventory().inventory(source)

    assert [candidate.key for candidate in candidates] == ["figure-001", "figure-002"]
    assert [candidate.provenance.occurrence_index for candidate in candidates] == [0, 1]
    assert candidates[0].media_sha256 == candidates[1].media_sha256


@pytest.mark.asyncio
async def test_unicode_alt_and_title_attributes_are_preserved() -> None:
    source = _source(
        '<img src="diagram.png" alt="Schéma 東京 &amp; réseau" title="État &quot;A&quot;">',
        _asset("diagram.png"),
    )

    (candidate,) = await ArchivedHtmlSourceFigureInventory().inventory(source)

    assert candidate.provenance.alt_text == "Schéma 東京 & réseau"
    assert candidate.provenance.title_text == 'État "A"'


@pytest.mark.asyncio
async def test_remote_and_scheme_references_are_ignored() -> None:
    source = _source(
        '<img src="https://example.test/a.png">'
        '<img src="http://example.test/b.png">'
        '<img src="data:image/png;base64,AA==">'
        '<img src="file:///tmp/c.png">'
        '<img src="custom:value">'
        '<img src="//cdn.example.test/d.png">'
        '<img src=" https://example.test/e.png ">'
        '<img src=""><img src="local.png">',
        _asset("local.png"),
    )

    candidates = await ArchivedHtmlSourceFigureInventory().inventory(source)

    assert len(candidates) == 1
    assert candidates[0].key == "figure-001"
    assert candidates[0].provenance.source_locator == "local.png"
    assert candidates[0].provenance.occurrence_index == 8


@pytest.mark.asyncio
async def test_src_whitespace_is_stripped_and_first_duplicate_attribute_wins() -> None:
    source = _source(
        '<img src="\n images/a.png\t" src="images/other.png">',
        _asset("images/a.png"),
    )

    (candidate,) = await ArchivedHtmlSourceFigureInventory().inventory(source)

    assert candidate.provenance.source_locator == "images/a.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("locator", ["../secret.png", "images/../../secret.png"])
async def test_parent_traversal_is_rejected(locator: str) -> None:
    source = _source(f'<img src="{locator}">')

    with pytest.raises(SourceFigureInventoryError, match="safe archive-relative path"):
        await ArchivedHtmlSourceFigureInventory().inventory(source)


@pytest.mark.asyncio
async def test_missing_local_asset_raises_inventory_error() -> None:
    source = _source('<img src="missing.png">')

    with pytest.raises(SourceFigureInventoryError, match="asset is missing"):
        await ArchivedHtmlSourceFigureInventory().inventory(source)


@pytest.mark.asyncio
async def test_identical_inventories_return_equal_candidate_tuples() -> None:
    source = _source(
        '<img src="a.png" alt="A"><img src="a.png" alt="A again">',
        _asset("a.png", b"the exact archived bytes"),
    )
    inventory = ArchivedHtmlSourceFigureInventory()

    first = await inventory.inventory(source)
    second = await inventory.inventory(source)

    assert first == second


@pytest.mark.asyncio
async def test_unsupported_document_media_type_is_rejected() -> None:
    source = _source("<img src='a.png'>", media_type="application/xhtml+xml")

    with pytest.raises(UnsupportedSourceFigureFormatError):
        await ArchivedHtmlSourceFigureInventory().inventory(source)

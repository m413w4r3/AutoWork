from __future__ import annotations

import base64
import hashlib
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from cti_app.application.source_figure_inventory import (
    ArchivedFigureAsset,
    ArchivedFigureSource,
    SourceFigureInventory,
)
from cti_app.domain.production_editorial_enrichment import (
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    source_figure_id,
)

_DOCUMENT_ID = UUID("10000000-0000-0000-0000-000000000001")
_IMAGE_DOCUMENT_ID = UUID("20000000-0000-0000-0000-000000000002")
_BLOB_ID = UUID("30000000-0000-0000-0000-000000000003")
_PNG = b"\x89PNG\r\n\x1a\nfigure-bytes"


def _source(
    content: bytes,
    *,
    source_document_id: UUID = _DOCUMENT_ID,
    source_url: str = "https://publisher.test/reports/sample.html",
    mime_type: str = "text/html",
) -> ArchivedFigureSource:
    return ArchivedFigureSource(
        source_document_id=source_document_id,
        source_url=source_url,
        mime_type=mime_type,
        blob_id=UUID(int=source_document_id.int + 100),
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        content=content,
    )


def _asset(
    *,
    source_url: str | None = "https://publisher.test/reports/figures/diagram.png",
    content: bytes = _PNG,
) -> ArchivedFigureAsset:
    return ArchivedFigureAsset(
        source_document_id=_IMAGE_DOCUMENT_ID,
        source_url=source_url,
        blob_id=_BLOB_ID,
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/png",
        byte_size=len(content),
    )


def _html(*images: str) -> bytes:
    return ("<html><body>" + "".join(images) + "</body></html>").encode()


def _data_uri(content: bytes = _PNG) -> str:
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


def test_data_uri_is_parsed_but_pending_without_an_existing_blob() -> None:
    source = _source(_html(f'<img src="{_data_uri()}" alt="Diagram">'))

    result = SourceFigureInventory().inventory((source,), ())

    assert len(result.figures) == 1
    figure = result.figures[0]
    assert figure.decision is SourceFigureDecision.PENDING
    assert figure.decision_reason == "image_bytes_not_in_blob_store"
    assert figure.blob_id is None
    assert figure.sha256 == hashlib.sha256(_PNG).hexdigest()
    assert figure.mime_type == "image/png"
    assert figure.byte_size == len(_PNG)


def test_data_uri_resolves_only_when_matching_image_blob_is_already_archived() -> None:
    source = _source(_html(f'<img src="{_data_uri()}" alt="Diagram">'))
    asset = _asset(source_url=None)

    result = SourceFigureInventory().inventory((source,), (asset,))

    assert len(result.accepted) == 1
    assert result.accepted[0].blob_id == _BLOB_ID
    assert result.accepted[0].decision_reason == "matched_archived_blob"


def test_relative_image_url_is_resolved_against_the_archived_page_only() -> None:
    source = _source(_html('<img src="figures/diagram.png" alt="Network map">'))

    result = SourceFigureInventory().inventory((source,), ())

    figure = result.figures[0]
    assert figure.decision is SourceFigureDecision.PENDING
    assert figure.locator.original_asset_url == (
        "https://publisher.test/reports/figures/diagram.png"
    )


def test_relative_image_url_can_match_an_existing_archived_blob() -> None:
    source = _source(_html('<img src="figures/diagram.png" alt="Network map">'))

    result = SourceFigureInventory().inventory((source,), (_asset(),))

    assert len(result.accepted) == 1
    assert result.accepted[0].blob_id == _BLOB_ID


def test_external_image_url_stays_pending_without_a_network_fetch() -> None:
    source = _source(_html('<img src="https://cdn.publisher.test/map.png">'))

    result = SourceFigureInventory().inventory((source,), ())

    assert len(result.figures) == 1
    assert result.figures[0].decision is SourceFigureDecision.PENDING
    assert result.figures[0].decision_reason == "image_not_in_local_archive"
    assert result.figures[0].blob_id is None


def test_duplicate_image_bytes_are_deduplicated_by_sha256() -> None:
    src = _data_uri()
    source = _source(_html(f'<img src="{src}"><img src="{src}">'))

    result = SourceFigureInventory().inventory((source,), ())

    assert len(result.figures) == 1
    assert result.figures[0].sha256 == hashlib.sha256(_PNG).hexdigest()


def test_inventory_applies_figure_count_and_size_limits() -> None:
    first = _data_uri(_PNG)
    second_bytes = b"\x89PNG\r\n\x1a\nsecond"
    second = _data_uri(second_bytes)
    source = _source(_html(f'<img src="{first}"><img src="{second}">'))

    limited = SourceFigureInventory(max_figures=1).inventory((source,), ())
    oversized = SourceFigureInventory(max_figure_bytes=8).inventory(
        (_source(_html(f'<img src="{first}">')),), ()
    )

    assert limited.truncated is True
    assert len(limited.figures) == 1
    assert oversized.figures[0].decision is SourceFigureDecision.REJECTED
    assert oversized.figures[0].decision_reason == "figure_exceeds_byte_limit"


def test_inventory_applies_its_total_byte_limit() -> None:
    second = b"\x89PNG\r\n\x1a\nother-figure"
    source = _source(_html(f'<img src="{_data_uri()}"><img src="{_data_uri(second)}">'))
    max_total = len(_PNG) + 1

    result = SourceFigureInventory(max_total_figure_bytes=max_total).inventory((source,), ())

    by_sha256 = {figure.sha256: figure for figure in result.figures}
    assert len(by_sha256) == 2
    assert by_sha256[hashlib.sha256(_PNG).hexdigest()].decision is SourceFigureDecision.PENDING
    rejected = by_sha256[hashlib.sha256(second).hexdigest()]
    assert rejected.decision is SourceFigureDecision.REJECTED
    assert rejected.decision_reason == "inventory_exceeds_total_byte_limit"


def test_inventory_order_is_stable_when_archived_inputs_are_reordered() -> None:
    first = _source(
        _html('<img src="figures/a.png">'),
        source_document_id=_DOCUMENT_ID,
        source_url="https://publisher.test/a.html",
    )
    second = _source(
        _html('<img src="figures/b.png">'),
        source_document_id=UUID("10000000-0000-0000-0000-000000000002"),
        source_url="https://publisher.test/b.html",
    )
    assets = (
        _asset(source_url="https://publisher.test/b/figures/b.png", content=b"\x89PNG\r\nb"),
        _asset(source_url="https://publisher.test/a/figures/a.png", content=b"\x89PNG\r\na"),
    )
    inventory = SourceFigureInventory()

    forward = inventory.inventory((first, second), assets)
    reversed_inputs = inventory.inventory((second, first), tuple(reversed(assets)))

    assert forward == reversed_inputs


def test_resolved_source_figure_requires_strict_complete_accepted_metadata() -> None:
    locator = SourceFigureLocatorV1(page=1)
    kwargs = {
        "figure_id": source_figure_id(
            source_document_id=_DOCUMENT_ID,
            sha256=hashlib.sha256(_PNG).hexdigest(),
            source="https://publisher.test/reports/sample.html",
            locator=locator,
        ),
        "blob_id": _BLOB_ID,
        "sha256": hashlib.sha256(_PNG).hexdigest(),
        "mime_type": "image/png",
        "byte_size": len(_PNG),
        "source_document_id": _DOCUMENT_ID,
        "source": "https://publisher.test/reports/sample.html",
        "provenance": "Archived image asset 20000000-0000-0000-0000-000000000002",
        "locator": locator,
        "decision": SourceFigureDecision.ACCEPTED,
        "decision_reason": "matched_archived_blob",
    }

    figure = ResolvedSourceFigureV1.model_validate(kwargs)

    assert figure.decision is SourceFigureDecision.ACCEPTED
    with pytest.raises(ValidationError):
        ResolvedSourceFigureV1.model_validate({**kwargs, "byte_size": str(len(_PNG))})
    with pytest.raises(ValidationError):
        ResolvedSourceFigureV1.model_validate({**kwargs, "mime_type": "image/bmp"})
    with pytest.raises(ValidationError):
        mutable_figure: Any = figure
        mutable_figure.decision = SourceFigureDecision.PENDING

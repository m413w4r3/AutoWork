from __future__ import annotations

import base64
import hashlib
import struct
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from cti_app.application.source_figure_inventory import (
    ArchivedFigureAsset,
    ArchivedFigureSource,
    SourceFigureInventory,
)
from cti_app.application.source_media_extraction import extract_source_media_observations
from cti_app.domain.production_editorial_enrichment import (
    ResolvedSourceFigureV1,
    SourceFigureDecision,
    SourceFigureLocatorV1,
    source_figure_id,
)
from cti_app.domain.source_media import (
    SourceFigureProvenanceStage,
    SourceMediaReasonCode,
    SourceMediaRecord,
    SourceMediaStatus,
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
    width: int | None = None,
    height: int | None = None,
) -> ArchivedFigureAsset:
    return ArchivedFigureAsset(
        source_document_id=_IMAGE_DOCUMENT_ID,
        source_url=source_url,
        blob_id=_BLOB_ID,
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/png",
        byte_size=len(content),
        width=width,
        height=height,
    )


def _html(*images: str) -> bytes:
    return ("<html><body>" + "".join(images) + "</body></html>").encode()


def _data_uri(content: bytes = _PNG) -> str:
    return "data:image/png;base64," + base64.b64encode(content).decode("ascii")


def _png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    )


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


def test_collection_failure_remains_pending_in_the_figure_inventory() -> None:
    source = _source(_html('<img src="/figures/chart.png" alt="Chart">'))
    (observation,) = extract_source_media_observations((source,))
    failed_collection = SourceMediaRecord(
        subject_id=_DOCUMENT_ID,
        source_document_id=_DOCUMENT_ID,
        policy_version="source-media-v1",
        policy_sha256="b" * 64,
        status=SourceMediaStatus.COLLECTION_FAILED,
        reason_code=SourceMediaReasonCode.COLLECTION_UNAVAILABLE,
        dom_locator=observation.dom_locator,
    )

    result = SourceFigureInventory().inventory((source,), (), media_candidates=(failed_collection,))

    assert len(result.figures) == 1
    assert result.figures[0].decision is SourceFigureDecision.PENDING
    assert result.figures[0].decision_reason == "collection_unavailable"
    assert any(
        item.stage is SourceFigureProvenanceStage.DOWNLOAD_FAILED
        for item in result.provenance_diagnostics
    )


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


@pytest.mark.parametrize(("width", "height"), [(10_001, 200), (8_000, 6_000)])
def test_inventory_rejects_header_only_pngs_with_excessive_dimensions(
    width: int, height: int
) -> None:
    content = _png_header(width, height)
    source = _source(_html(f'<img src="{_data_uri(content)}" alt="Oversized chart">'))

    result = SourceFigureInventory().inventory((source,), ())

    assert len(result.figures) == 1
    assert result.figures[0].decision is SourceFigureDecision.REJECTED
    assert result.figures[0].decision_reason == "image_too_large_dimensions"


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


def test_inventory_accepts_wide_archived_figure_and_marks_aspect_warning() -> None:
    image = _png_header(1200, 100)
    source = _source(_html('<img src="figures/wide.png" alt="Wide chart">'))
    asset = _asset(
        source_url="https://publisher.test/reports/figures/wide.png",
        content=image,
        width=1200,
        height=100,
    )

    result = SourceFigureInventory().inventory((source,), (asset,))

    figure = result.figures[0]
    assert figure.decision is SourceFigureDecision.ACCEPTED
    assert figure.blob_id == _BLOB_ID
    assert result.catalog_metadata[figure.figure_id].aspect_ratio_warning is True
    assert tuple(item.stage for item in result.provenance_diagnostics) == (
        SourceFigureProvenanceStage.DISCOVERED,
        SourceFigureProvenanceStage.ARCHIVED,
    )


def test_bluemoon_exploitation_chain_is_collectable_with_article_provenance() -> None:
    article_url = "https://publisher.test/reports/bluemoon.html"
    asset_url = "https://cdn.publisher.test/bluemoon-exploitation-chain.png"
    source = _source(
        _html(
            "<article><h2>BlueMoon exploitation chain</h2>"
            "<p>The lure launches the loader.</p><figure>"
            f'<img src="{asset_url}" alt="BlueMoon exploitation chain">'
            "<figcaption>From lure to post-exploitation payloads.</figcaption>"
            "</figure><p>The loader deploys the final payload.</p></article>"
        ),
        source_url=article_url,
    )
    archived_bytes = _png_header(1800, 800)
    asset = _asset(source_url=asset_url, content=archived_bytes, width=1800, height=800)

    result = SourceFigureInventory().inventory((source,), (asset,))

    (figure,) = result.accepted
    metadata = result.catalog_metadata[figure.figure_id]
    assert figure.blob_id == _BLOB_ID
    assert figure.source == article_url
    assert figure.locator.original_asset_url == asset_url
    assert metadata.alt_text == "BlueMoon exploitation chain"
    assert metadata.caption_text == "From lure to post-exploitation payloads."
    assert metadata.nearby_heading_text == "BlueMoon exploitation chain"
    assert metadata.context_before == "The lure launches the loader."
    assert metadata.context_after == "The loader deploys the final payload."
    assert [item.stage for item in result.provenance_diagnostics] == [
        SourceFigureProvenanceStage.DISCOVERED,
        SourceFigureProvenanceStage.ARCHIVED,
    ]


def test_logo_is_rejected_as_editorial_and_kept_in_provenance_diagnostics() -> None:
    source = _source(_html('<div><img class="logo" src="/logo.png" alt="Publisher logo"></div>'))

    result = SourceFigureInventory().inventory((source,), ())

    (figure,) = result.figures
    assert figure.decision is SourceFigureDecision.REJECTED
    assert figure.decision_reason == SourceMediaReasonCode.BOILERPLATE_PATTERN.value
    assert tuple(item.stage for item in result.provenance_diagnostics) == (
        SourceFigureProvenanceStage.DISCOVERED,
        SourceFigureProvenanceStage.REJECTED_EDITORIAL,
    )


@pytest.mark.parametrize(
    ("status", "reason_code", "expected_stage"),
    (
        (
            SourceMediaStatus.EXCLUDED_BY_RULE,
            SourceMediaReasonCode.UNSAFE_DESTINATION,
            SourceFigureProvenanceStage.REJECTED_SECURITY,
        ),
        (
            SourceMediaStatus.COLLECTION_FAILED,
            SourceMediaReasonCode.UNSAFE_DESTINATION,
            SourceFigureProvenanceStage.REJECTED_SECURITY,
        ),
        (
            SourceMediaStatus.EXCLUDED_BY_RULE,
            SourceMediaReasonCode.UNSUPPORTED_IMAGE_TYPE,
            SourceFigureProvenanceStage.REJECTED_FORMAT,
        ),
    ),
)
def test_security_and_format_rejections_are_structured(
    status: SourceMediaStatus,
    reason_code: SourceMediaReasonCode,
    expected_stage: SourceFigureProvenanceStage,
) -> None:
    source = _source(_html('<img src="https://cdn.publisher.test/asset.png" alt="Candidate">'))
    (observation,) = extract_source_media_observations((source,))
    rejected = SourceMediaRecord(
        subject_id=_DOCUMENT_ID,
        source_document_id=_DOCUMENT_ID,
        policy_version="source-media-v1",
        policy_sha256="b" * 64,
        status=status,
        reason_code=reason_code,
        dom_locator=observation.dom_locator,
    )

    result = SourceFigureInventory().inventory((source,), (), media_candidates=(rejected,))

    assert result.figures[0].decision is (
        SourceFigureDecision.PENDING
        if status is SourceMediaStatus.COLLECTION_FAILED
        else SourceFigureDecision.REJECTED
    )
    assert any(item.stage is expected_stage for item in result.provenance_diagnostics)


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


def test_alt_less_image_keeps_the_text_around_it_for_the_model_to_recognise_it() -> None:
    source = _source(
        _html(
            "<p>Before words.</p><p>The attacker rotates infrastructure.</p>"
            '<p><img src="https://cdn.publisher.test/shot-1.png"></p>'
            "<p>After words.</p>"
        )
    )

    result = SourceFigureInventory().inventory((source,), ())

    (metadata,) = result.catalog_metadata.values()
    assert metadata.context_before == "The attacker rotates infrastructure."
    assert metadata.context_after == "After words."


def test_inventory_prefers_article_figure_and_excludes_recent_cards_and_modal_icons() -> None:
    article_url = "https://cdn.publisher.test/article-flow.png"
    html = _html(
        "<article><h1>Bitcoin dead drop resolution</h1><figure>"
        f'<img src="{article_url}"></figure><p>The article explains the flow.</p></article>'
        '<section><h2>Recent posts</h2><div class="recent-posts-grid">'
        '<img alt="Unrelated report A" width="1200" height="630" src="/recent-a.jpg">'
        '<img alt="Unrelated report B" width="1200" height="630" src="/recent-b.jpg">'
        "</div></section>"
        '<div role="dialog"><button><img class="modal-close" '
        'alt="Close this modal" src="/close-icon.svg"></button></div>'
    )
    source = _source(html)
    article_asset = _asset(source_url=article_url)

    result = SourceFigureInventory().inventory((source,), (article_asset,))

    assert len(result.accepted) == 1
    article = result.accepted[0]
    assert result.catalog_metadata[article.figure_id].in_article_body is True
    recent = [item for item in result.figures if item.decision_reason == "related_content_card"]
    assert len(recent) == 2
    assert all(item.decision is SourceFigureDecision.REJECTED for item in recent)
    modal = next(item for item in result.figures if item.locator.figure_label == "Close this modal")
    assert modal.decision is SourceFigureDecision.REJECTED
    assert modal.decision_reason == "decorative_asset"


def test_inventory_cap_keeps_article_body_images_before_page_chrome() -> None:
    first_url = "https://cdn.publisher.test/article-one.png"
    second_url = "https://cdn.publisher.test/article-two.png"
    source = _source(
        _html(
            '<header><img src="/logo.png" alt="Publisher logo"></header>'
            f'<article><img src="{first_url}"><img src="{second_url}"></article>'
        )
    )
    assets = (
        _asset(source_url=first_url, content=_PNG + b"first"),
        _asset(source_url=second_url, content=_PNG + b"second"),
    )

    result = SourceFigureInventory(max_figures=2).inventory((source,), assets)

    assert result.truncated is True
    assert len(result.accepted) == 2
    assert all(result.catalog_metadata[item.figure_id].in_article_body for item in result.accepted)

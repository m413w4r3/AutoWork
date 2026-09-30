from __future__ import annotations

import asyncio
import hashlib
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from cti_app.application.source_figure_inventory import (
    ArchivedSourceAsset,
    ArchivedSourceDocumentSnapshot,
    SourceFigureInventory,
)
from cti_app.domain.production_source_figures import (
    SourceFigureCandidateV1,
    SourceFigureOriginKind,
    SourceFigureProvenanceV1,
)


def _asset(path: str) -> ArchivedSourceAsset:
    return ArchivedSourceAsset(path, "image/png", b"image bytes")


def test_snapshot_normalizes_paths_and_preserves_asset_order() -> None:
    first = _asset("images//./first.png")
    second = _asset("./second.png")

    snapshot = ArchivedSourceDocumentSnapshot(
        uuid4(), "text/html", b"<html></html>", (first, second)
    )

    assert first.path == "images/first.png"
    assert second.path == "second.png"
    assert snapshot.local_assets == (first, second)
    with pytest.raises(FrozenInstanceError):
        snapshot.body_bytes = b"changed"  # type: ignore[misc]


def test_snapshot_rejects_duplicate_normalized_asset_paths() -> None:
    with pytest.raises(ValueError, match="unique"):
        ArchivedSourceDocumentSnapshot(
            uuid4(), "text/html", b"", (_asset("images//figure.png"), _asset("images/figure.png"))
        )


@pytest.mark.parametrize(
    "path",
    [
        "../figure.png",
        "images/../figure.png",
        "/figure.png",
        "https://host/figure.png",
        "C:/figure.png",
        r"..\figure.png",
    ],
)
def test_asset_rejects_traversal_and_absolute_or_url_paths(path: str) -> None:
    with pytest.raises(ValueError):
        _asset(path)


@pytest.mark.parametrize("media_type", ["text/plain", "image/", ""])
def test_asset_rejects_non_image_media_types(media_type: str) -> None:
    with pytest.raises(ValueError):
        ArchivedSourceAsset("figure.png", media_type, b"image bytes")


def test_asset_rejects_empty_or_non_bytes_content() -> None:
    with pytest.raises(ValueError):
        ArchivedSourceAsset("figure.png", "image/png", b"")
    with pytest.raises(ValueError):
        ArchivedSourceAsset("figure.png", "image/png", bytearray(b"image bytes"))  # type: ignore[arg-type]


def test_snapshot_requires_tuple_assets() -> None:
    with pytest.raises(ValueError, match="tuple"):
        ArchivedSourceDocumentSnapshot(
            uuid4(),
            "text/html",
            b"",
            [_asset("figure.png")],  # type: ignore[arg-type]
        )


def test_inventory_protocol_returns_immutable_candidate_tuple() -> None:
    source_document_id = uuid4()
    candidate_bytes = b"figure"
    candidate = SourceFigureCandidateV1(
        key="figure-1",
        source_document_id=source_document_id,
        media_type="image/png",
        media_bytes=candidate_bytes,
        media_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
        provenance=SourceFigureProvenanceV1(
            SourceFigureOriginKind.HTML_IMAGE, "images/figure.png", 0
        ),
    )

    class FakeInventory:
        async def inventory(
            self, source: ArchivedSourceDocumentSnapshot
        ) -> tuple[SourceFigureCandidateV1, ...]:
            assert source.source_document_id == source_document_id
            return (candidate,)

    implementation = FakeInventory()
    assert isinstance(implementation, SourceFigureInventory)
    result = asyncio.run(
        implementation.inventory(
            ArchivedSourceDocumentSnapshot(source_document_id, "text/html", b"", ())
        )
    )
    assert result == (candidate,)
    assert isinstance(result, tuple)

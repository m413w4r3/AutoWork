import hashlib
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from cti_app.domain.production_source_figures import (
    SourceFigureCandidateV1,
    SourceFigureOriginKind,
    SourceFigureProvenanceV1,
)


def _provenance(**overrides: object) -> SourceFigureProvenanceV1:
    values: dict[str, object] = {
        "origin_kind": SourceFigureOriginKind.HTML_IMAGE,
        "source_locator": "/article/img[0]",
        "occurrence_index": 0,
        "alt_text": "A source image",
        "title_text": "Image title",
    }
    values.update(overrides)
    return SourceFigureProvenanceV1(**values)  # type: ignore[arg-type]


def _candidate(**overrides: object) -> SourceFigureCandidateV1:
    media_bytes = b"\x89PNG\r\n\x1a\nexact image bytes"
    values: dict[str, object] = {
        "key": "source-figure-0",
        "source_document_id": uuid4(),
        "media_type": "image/png",
        "media_bytes": media_bytes,
        "media_sha256": hashlib.sha256(media_bytes).hexdigest(),
        "provenance": _provenance(),
    }
    values.update(overrides)
    return SourceFigureCandidateV1(**values)  # type: ignore[arg-type]


def test_candidate_preserves_exact_bytes_and_matching_sha256() -> None:
    media_bytes = b"original bytes\x00\xff"
    candidate = _candidate(
        media_bytes=media_bytes,
        media_sha256=hashlib.sha256(media_bytes).hexdigest(),
    )

    assert candidate.media_bytes is media_bytes
    assert candidate.media_sha256 == hashlib.sha256(media_bytes).hexdigest()
    assert candidate.provenance.origin_kind is SourceFigureOriginKind.HTML_IMAGE


def test_candidate_rejects_hash_that_does_not_match_bytes() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _candidate(media_sha256="0" * 64)


@pytest.mark.parametrize("malformed", ["A" * 64, "f" * 63, "g" * 64, " "])
def test_candidate_rejects_noncanonical_hash(malformed: str) -> None:
    with pytest.raises(ValueError, match="canonical lowercase"):
        _candidate(media_sha256=malformed)


def test_candidate_rejects_empty_bytes_and_non_image_media_type() -> None:
    with pytest.raises(ValueError, match="non-empty bytes"):
        _candidate(media_bytes=b"")
    with pytest.raises(ValueError, match="image MIME type"):
        _candidate(media_type="application/octet-stream")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_document_id", "not-a-uuid"),
        ("source_document_id", None),
        ("media_type", None),
        ("media_bytes", bytearray(b"image")),
        ("provenance", "html_image"),
        ("key", "  "),
    ],
)
def test_candidate_rejects_invalid_field_types_or_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _candidate(**{field: value})


@pytest.mark.parametrize("locator", ["", " \t\n"])
def test_provenance_rejects_empty_locator(locator: str) -> None:
    with pytest.raises(ValueError, match="non-empty text"):
        _provenance(source_locator=locator)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin_kind", "html_image"),
        ("source_locator", None),
        ("occurrence_index", True),
        ("occurrence_index", -1),
        ("alt_text", 1),
        ("title_text", 1),
    ],
)
def test_provenance_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _provenance(**{field: value})


def test_source_figure_values_are_frozen() -> None:
    provenance = _provenance()
    candidate = _candidate(provenance=provenance)

    with pytest.raises(FrozenInstanceError):
        provenance.source_locator = "/changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        candidate.key = "changed"  # type: ignore[misc]

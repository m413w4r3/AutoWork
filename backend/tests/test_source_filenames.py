from datetime import date
from typing import Any

import pytest

from cti_app.application.source_filenames import (
    analyst_filename,
    ascii_download_filename,
    extension_for_mime,
    validate_logical_filename,
)
from cti_app.domain.classification import TLP


@pytest.mark.parametrize(
    ("mime_type", "extension"),
    [
        ("text/html", ".html"),
        ("application/pdf", ".pdf"),
        ("text/plain", ".txt"),
        ("application/json", ".json"),
        ("application/octet-stream", ".bin"),
    ],
)
def test_extension_is_derived_from_detected_mime(mime_type: str, extension: str) -> None:
    assert extension_for_mime(mime_type) == extension


def test_analyst_filename_uses_business_metadata_and_unicode() -> None:
    result = analyst_filename(
        published_at=date(2026, 7, 28),
        tlp=TLP.AMBER,
        title="Cyberattaque coordonnée sur l\u2019eau",
        publisher="Équipe Spéciale",
        detected_mime_type="text/html",
        decoded_sha256="a" * 64,
    )

    assert result == (
        "2026-07-28_TLP AMBER_Cyberattaque coordonnée sur l\u2019eau_Équipe Spéciale.html"
    )
    assert "*" not in result


def test_unknown_values_and_unsafe_characters_are_sanitized_deterministically() -> None:
    arguments: dict[str, Any] = {
        "published_at": None,
        "tlp": TLP.RED,
        "title": '../Rapport\x00 / \\ : * ? " < > |',
        "publisher": None,
        "detected_mime_type": "application/pdf",
        "decoded_sha256": "b" * 64,
    }
    first = analyst_filename(**arguments)
    second = analyst_filename(**arguments)

    assert first == second
    assert first.startswith("date-inconnue_TLP RED_")
    assert "publisher-inconnu" in first
    assert not any(
        value in first for value in ("..", "/", "\\", ":", "*", "?", '"', "<", ">", "|", "\x00")
    )
    assert first.endswith(".pdf")


def test_long_filename_preserves_extension_and_utf8_boundary() -> None:
    result = analyst_filename(
        published_at=None,
        tlp=TLP.AMBER_STRICT,
        title="é" * 500,
        publisher="éditeur",
        detected_mime_type="application/json",
        decoded_sha256="c" * 64,
    )

    assert len(result.encode("utf-8")) <= 240
    assert result.endswith(".json")


def test_collision_adds_decoded_sha256_suffix_before_extension() -> None:
    base = analyst_filename(
        published_at=date(2026, 7, 28),
        tlp=TLP.AMBER,
        title="Rapport",
        publisher="Example",
        detected_mime_type="text/html",
        decoded_sha256="32d872d1" + "0" * 56,
    )
    collision = analyst_filename(
        published_at=date(2026, 7, 28),
        tlp=TLP.AMBER,
        title="Rapport",
        publisher="Example",
        detected_mime_type="text/html",
        decoded_sha256="32d872d1" + "0" * 56,
        existing_names={base},
    )

    assert collision.endswith("__32d872d1.html")


def test_logical_filename_rejects_paths_and_ascii_fallback_keeps_extension() -> None:
    with pytest.raises(ValueError):
        validate_logical_filename("../rapport.html")
    assert ascii_download_filename("État de l\u2019été.html") == "Etat de lete.html"

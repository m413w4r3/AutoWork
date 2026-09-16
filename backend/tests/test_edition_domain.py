from datetime import date

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.editions import (
    Edition,
    EditionImmutableError,
    EditionStatus,
)


def make_edition() -> Edition:
    return Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
    )


def test_creation_starts_open() -> None:
    edition = make_edition()

    assert edition.state is EditionStatus.OPEN
    assert not hasattr(edition, "status")


def test_month_period_and_language_validation() -> None:
    with pytest.raises(ValueError, match="complete calendar month"):
        Edition(
            country="Iran",
            country_code="IR",
            period_start=date(2026, 7, 2),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr",),
        )


def test_open_edition_can_be_modified() -> None:
    edition = make_edition()

    edition.update_metadata(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
    )


def test_tlp_downgrade_is_rejected() -> None:
    edition = make_edition()

    with pytest.raises(ValueError):
        edition.update_metadata(
            country="Iran",
            country_code="IR",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.GREEN,
            languages=("fr", "en", "fa"),
        )


def test_archiving_is_terminal_and_advances_version() -> None:
    edition = make_edition()
    initial_version = edition.version

    edition.archive()

    assert edition.state is EditionStatus.ARCHIVED
    assert edition.version == initial_version + 1
    with pytest.raises(EditionImmutableError):
        edition.archive()


def test_archived_edition_rejects_metadata_updates() -> None:
    edition = make_edition()
    edition.archive()

    with pytest.raises(EditionImmutableError):
        edition.update_metadata(
            country="Iran",
            country_code="IR",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr", "en", "fa"),
        )


def test_progression_primitives_are_absent() -> None:
    import cti_app.domain.editions as editions

    assert not hasattr(editions, "EDITION_TRANSITIONS")
    assert not hasattr(editions, "EDITION_PROGRESS")
    assert not hasattr(editions, "InvalidEditionTransitionError")
    assert not hasattr(Edition, "allowed_transitions")
    assert not hasattr(Edition, "progress_percent")
    assert not hasattr(Edition, "transition")
    assert not hasattr(Edition, "return_to_selection_after_production_cancellation")

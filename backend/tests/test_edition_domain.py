from datetime import date

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.editions import (
    Edition,
    EditionImmutableError,
    EditionStatus,
    InvalidEditionTransitionError,
)


def make_edition() -> Edition:
    return Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
        target_major_articles=2,
        target_briefs=6,
        source_profile="iran-default",
    )


def test_month_period_and_language_validation() -> None:
    with pytest.raises(ValueError, match="complete calendar month"):
        Edition(
            country="Iran",
            country_code="IR",
            period_start=date(2026, 7, 2),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr",),
            target_major_articles=2,
            target_briefs=6,
            source_profile="iran-default",
        )


def test_state_machine_exposes_only_valid_actions() -> None:
    edition = make_edition()
    assert edition.allowed_transitions == (EditionStatus.DISCOVERY, EditionStatus.ARCHIVED)

    edition.transition(EditionStatus.DISCOVERY)

    assert edition.status is EditionStatus.DISCOVERY
    assert edition.progress_percent == 15
    with pytest.raises(InvalidEditionTransitionError):
        edition.transition(EditionStatus.PUBLISHED)


def test_assembling_cannot_be_archived_but_published_can() -> None:
    edition = make_edition()
    edition.status = EditionStatus.ASSEMBLING
    assert edition.allowed_transitions == (
        EditionStatus.REVIEW,
        EditionStatus.PUBLISHED,
    )

    with pytest.raises(InvalidEditionTransitionError):
        edition.transition(EditionStatus.ARCHIVED)

    edition.status = EditionStatus.PUBLISHED
    edition.transition(EditionStatus.ARCHIVED)

    assert edition.status is EditionStatus.ARCHIVED


@pytest.mark.parametrize(
    "status",
    (EditionStatus.ASSEMBLING, EditionStatus.PUBLISHED, EditionStatus.ARCHIVED),
)
def test_frozen_editions_reject_metadata_updates(status: EditionStatus) -> None:
    edition = make_edition()
    edition.status = status

    with pytest.raises(EditionImmutableError):
        edition.update_metadata(
            country="Iran",
            country_code="IR",
            period_start=date(2026, 7, 1),
            period_end=date(2026, 7, 31),
            tlp=TLP.AMBER,
            languages=("fr", "en", "fa"),
            target_major_articles=3,
            target_briefs=7,
            previous_edition_id=None,
            source_profile="iran-default",
        )


def test_review_editions_allow_metadata_updates() -> None:
    edition = make_edition()
    edition.status = EditionStatus.REVIEW

    edition.update_metadata(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
        target_major_articles=3,
        target_briefs=7,
        previous_edition_id=None,
        source_profile="iran-default",
    )

    assert edition.target_briefs == 7
